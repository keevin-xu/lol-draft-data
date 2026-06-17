"""
TrackingThePros scraper — pulls all pro players + soloq rank data via the
site's internal DataTables JSON API and saves a daily snapshot.

Data source:  https://www.trackingthepros.com/players
API endpoint: https://www.trackingthepros.com/d/list_players  (public, no auth)

Strategy:
  1. Paginate the list_players API (page_size=200, ~12 calls for ~2300 players).
  2. Parse the `rankHigh` string into tier + LP + numeric soloq rating.
  3. Infer the primary server from `current_region`.
  4. Save full snapshot to data/raw/trackingthepros/YYYY-MM-DD.json.
  5. Upsert players → SQLite `players` table.
  6. Upsert best account → SQLite `accounts` table.

Note: TTP stores multiple accounts per player but only exposes them on
individual player pages (not in the list API). The `rankHigh` field already
gives the *highest* rank across all accounts — sufficient for the soloq
signal. Pass --full-accounts to also fetch individual pages and store every
summoner account per player (slower: ~2300 extra HTTP requests).

Run:
  python scrapers/ttp_scraper.py             # fast mode (list API only)
  python scrapers/ttp_scraper.py --full-accounts  # fetch every player page too
"""

import argparse
import json
import math
import re
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path
from typing import Optional

import requests
from loguru import logger
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).parent.parent
DB_PATH = _ROOT / "db" / "lol_model.db"
RAW_DIR = _ROOT / "data" / "raw" / "trackingthepros"

# ---------------------------------------------------------------------------
# TTP API constants
# ---------------------------------------------------------------------------
TTP_BASE = "https://www.trackingthepros.com"
LIST_ENDPOINT = f"{TTP_BASE}/d/list_players"
PAGE_SIZE = 200
REQUEST_DELAY = 0.5  # seconds between paginated API calls (be polite)

# DataTables column definitions required by the server-side endpoint
_DT_COLUMNS = {
    "columns[0][data]": "player_name",
    "columns[0][name]": "players.name",
    "columns[0][searchable]": "true",
    "columns[0][orderable]": "true",
    "columns[1][data][_]": "position",
    "columns[1][data][sort]": "positionNum",
    "columns[1][name]": "players.position",
    "columns[2][data]": "team_name",
    "columns[2][name]": "teams.name",
    "columns[3][data]": "current_region",
    "columns[3][name]": "players.current_region",
    "columns[4][data][_]": "rankHigh",
    "columns[4][data][sort]": "rankHighNum",
    "columns[4][name]": "players.highest_rank",
    "columns[5][data]": "player_accounts",
    "columns[5][name]": "player_accounts",
    "columns[6][data][_]": "online",
    "columns[6][data][sort]": "onlineNum",
    "columns[6][name]": "player_online",
    "order[0][column]": "4",
    "order[0][dir]": "desc",
    "search[value]": "",
    "search[regex]": "false",
}

# Map TTP region codes → LoL server IDs
REGION_TO_SERVER = {
    "EU":  "EUW",
    "NA":  "NA1",
    "KR":  "KR",
    "BR":  "BR1",
    "LAS": "LA2",
    "LAN": "LA1",
    "OCE": "OC1",
    "TR":  "TR1",
    "RU":  "RU",
    "JP":  "JP1",
    "PH":  "PH2",
    "SG":  "SG2",
    "TH":  "TH2",
    "TW":  "TW2",
    "VN":  "VN2",
}

# ---------------------------------------------------------------------------
# Rank parsing + numeric rating
# ---------------------------------------------------------------------------
TIER_ORDER = [
    "Iron", "Bronze", "Silver", "Gold", "Platinum",
    "Emerald", "Diamond", "Master", "Grandmaster", "Challenger",
]

# Base rating assigned to the *bottom* of each tier
# Divisions I–IV each add 100 LP worth of space, so:
#   Iron IV = 0, Iron III = 100, ..., Iron I = 300
#   Bronze IV = 400, ..., Diamond I = 2700
# Master/Grandmaster/Challenger share base 2800 + log-compressed LP
TIER_BASE = {
    "Iron":         0,
    "Bronze":       400,
    "Silver":       800,
    "Gold":         1200,
    "Platinum":     1600,
    "Emerald":      2000,
    "Diamond":      2400,
    "Master":       2800,
    "Grandmaster":  2800,
    "Challenger":   2800,
}

_DIVISION_OFFSET = {"I": 300, "II": 200, "III": 100, "IV": 0}


def rank_to_rating(tier: str, division: Optional[str], lp: int) -> float:
    """
    Convert rank components to a numeric rating on an ELO-adjacent scale.

    Master+ uses log compression so that 2000 LP Challenger is not
    50× better than 1 LP Master.
    """
    base = TIER_BASE.get(tier, 0)
    if tier in ("Master", "Grandmaster", "Challenger"):
        return base + 400 * math.log1p(lp / 400)
    div_offset = _DIVISION_OFFSET.get(division or "IV", 0)
    return base + div_offset + lp


def parse_rank(rank_str: str) -> dict:
    """
    Parse strings like:
      "Challenger 3,744LP"    → {tier: Challenger, division: None, lp: 3744}
      "Diamond I 72 LP"       → {tier: Diamond, division: I, lp: 72}
      "Ch 3,744LP"            → abbreviated form (TTP uses "Ch" for Challenger)
      "Unranked"              → {tier: Unranked, division: None, lp: 0}
    Returns dict with keys: tier, division, lp, rating.
    """
    ABBREV = {
        "Ch": "Challenger", "GM": "Grandmaster", "Ma": "Master",
        "D": "Diamond", "Em": "Emerald", "Pl": "Platinum",
        "G": "Gold", "S": "Silver", "B": "Bronze", "I": "Iron",
    }

    if not rank_str or rank_str.strip().lower() in ("unranked", "", "n/a"):
        return {"tier": "Unranked", "division": None, "lp": 0, "rating": 0.0}

    s = rank_str.strip()

    # Expand abbreviations at start ("Ch 3,744LP" → "Challenger 3,744LP")
    for abbr, full in ABBREV.items():
        if re.match(rf"^{abbr}\b", s):
            s = full + s[len(abbr):]
            break

    # Remove commas from numbers ("3,744" → "3744")
    s = s.replace(",", "")

    # Match tier, optional Roman numeral division (I/II/III/IV), optional LP.
    # Explicit IV|III|II|I ordering: greedy picks longest match first.
    # DIV_PAT uses non-empty alternatives only — avoids the I{0,3} empty-match
    # bug that causes the division group to consume whitespace without a division,
    # leaving no \s+ before the LP digits.
    TIER_PAT = (
        r"(Challenger|Grandmaster|Master|Diamond|Emerald|Platinum|Gold|Silver|Bronze|Iron)"
    )
    DIV_PAT = r"(?:\s+(IV|III|II|I))?"  # optional; only matches real divisions
    LP_PAT  = r"(?:\s*(\d+)\s*L?P)?"   # digits followed by optional "LP"/"P"
    m = re.match(TIER_PAT + DIV_PAT + LP_PAT, s, re.IGNORECASE)
    if not m or not m.group(1):
        return {"tier": "Unranked", "division": None, "lp": 0, "rating": 0.0}

    tier_map = {t.lower(): t for t in [
        "Challenger", "Grandmaster", "Master",
        "Diamond", "Emerald", "Platinum", "Gold", "Silver", "Bronze", "Iron",
    ]}
    tier = tier_map.get(m.group(1).lower(), m.group(1))
    division = (m.group(2) or "").upper().strip() or None
    lp = int(m.group(3)) if m.group(3) else 0

    return {
        "tier": tier,
        "division": division,
        "lp": lp,
        "rating": rank_to_rating(tier, division, lp),
    }


# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------
def _make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1.5,
        status_forcelist=[500, 502, 503, 504],
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({
        "Referer": f"{TTP_BASE}/players",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "X-Requested-With": "XMLHttpRequest",
    })
    return session


# ---------------------------------------------------------------------------
# List-API fetcher
# ---------------------------------------------------------------------------
def fetch_all_players(session: Optional[requests.Session] = None) -> list:
    """
    Paginate the /d/list_players DataTables endpoint and return all rows.
    """
    s = session or _make_session()
    all_rows = []
    start = 0
    total = None

    while True:
        params = {
            "draw": str(start // PAGE_SIZE + 1),
            "start": str(start),
            "length": str(PAGE_SIZE),
            **_DT_COLUMNS,
        }
        r = s.get(LIST_ENDPOINT, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()

        if total is None:
            total = int(data.get("recordsTotal", 0))
            logger.info(f"TTP reports {total} total players")

        rows = data.get("data", [])
        if not rows:
            break

        all_rows.extend(rows)
        logger.info(f"  Fetched {len(all_rows)}/{total} players (start={start})")

        start += len(rows)
        if start >= total:
            break

        time.sleep(REQUEST_DELAY)

    return all_rows


# ---------------------------------------------------------------------------
# Individual player page fetcher (for --full-accounts mode)
# ---------------------------------------------------------------------------
def fetch_player_accounts(
    player_name: str,
    session: Optional[requests.Session] = None,
    delay: float = 0.3,
) -> list:
    """
    Fetch a player's page and parse all summoner accounts from the HTML table.
    Returns a list of dicts: {summoner, server, rank_str, ...parsed rank...}
    """
    s = session or _make_session()
    url = f"{TTP_BASE}/player/{player_name}"
    try:
        r = s.get(url, timeout=15)
        r.raise_for_status()
    except Exception as e:
        logger.warning(f"Could not fetch player page for {player_name}: {e}")
        return []

    html = r.text
    # Find the accounts table: <td><b>[SERVER]</b> SummonerName#TAG</td><td>Rank</td>
    accounts = []
    pattern = re.compile(
        r"<td><b>\[([A-Z0-9]+)\]</b>\s*([^<]+)</td>\s*<td[^>]*>([^<]+)</td>",
        re.IGNORECASE,
    )
    for m in pattern.finditer(html):
        server = m.group(1).strip().upper()
        summoner = m.group(2).strip()
        rank_raw = m.group(3).strip()
        parsed = parse_rank(rank_raw)
        accounts.append({
            "summoner": summoner,
            "server": server,
            "rank_str": rank_raw,
            **parsed,
        })

    time.sleep(delay)
    return accounts


# ---------------------------------------------------------------------------
# Snapshot builder
# ---------------------------------------------------------------------------
def build_snapshot(raw_rows: list, full_accounts: bool = False,
                   session: Optional[requests.Session] = None) -> list:
    """
    Convert raw API rows into clean player dicts, optionally augmenting with
    full per-server account data fetched from individual player pages.
    """
    players = []
    for row in raw_rows:
        name_plug = row.get("name_plug") or row.get("name", "")
        rank_high = parse_rank(row.get("rankHigh", ""))
        inferred_server = REGION_TO_SERVER.get(
            str(row.get("current_region", "")).upper(), "???"
        )

        # Primary (best) account derived from list-API rankHigh
        primary_account = {
            "summoner": row.get("name", ""),  # placeholder; real name on player page
            "server": inferred_server,
            "rank_str": row.get("rankHigh", ""),
            **rank_high,
        }

        if full_accounts and row.get("player_accounts", 0) > 0:
            accounts = fetch_player_accounts(name_plug, session)
            if not accounts:
                accounts = [primary_account]
        else:
            accounts = [primary_account]

        players.append({
            "player_name": row.get("name", ""),
            "role": row.get("position", ""),
            "team": row.get("team_name", ""),
            "region": str(row.get("current_region", "")),
            "pro_level": row.get("pro_level", 0),
            "rank_high_str": row.get("rankHigh", ""),
            "rank_high_num": row.get("rankHighNum", 0),
            "accounts": accounts,
        })

    return players


# ---------------------------------------------------------------------------
# SQLite upsert
# ---------------------------------------------------------------------------
def upsert_players(snapshot: list, snapshot_date: str) -> tuple:
    """
    Upsert players + their accounts into SQLite.
    Returns (n_players_upserted, n_accounts_upserted).
    """
    conn = sqlite3.connect(DB_PATH)
    n_players = n_accounts = 0
    today = snapshot_date

    try:
        for p in snapshot:
            # Upsert player
            conn.execute(
                """
                INSERT INTO players (player_name, role, team, region, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(player_name) DO UPDATE SET
                    role       = excluded.role,
                    team       = excluded.team,
                    region     = excluded.region,
                    updated_at = excluded.updated_at
                """,
                (p["player_name"], p["role"], p["team"], p["region"], today),
            )
            n_players += 1

            # Look up the player_id we just inserted/found
            player_id = conn.execute(
                "SELECT id FROM players WHERE player_name = ?",
                (p["player_name"],),
            ).fetchone()[0]

            # Upsert accounts
            for acct in p.get("accounts", []):
                conn.execute(
                    """
                    INSERT INTO accounts
                        (player_id, summoner_name, server, rank_tier, lp,
                         soloq_rating, snapshot_date)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        player_id,
                        acct.get("summoner", ""),
                        acct.get("server", ""),
                        acct.get("tier", "Unranked"),
                        acct.get("lp", 0),
                        acct.get("rating", 0.0),
                        today,
                    ),
                )
                n_accounts += 1

        conn.commit()
    finally:
        conn.close()

    return n_players, n_accounts


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main(full_accounts: bool = False) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    today = str(date.today())
    session = _make_session()

    # 1. Fetch all players from list API
    logger.info("Fetching all players from TTP list API…")
    raw_rows = fetch_all_players(session)
    logger.info(f"Retrieved {len(raw_rows)} player rows")

    # 2. Build snapshot (optionally with full per-server accounts)
    if full_accounts:
        logger.info("Full-accounts mode: fetching individual player pages…")
    snapshot = build_snapshot(raw_rows, full_accounts=full_accounts, session=session)

    # 3. Save raw snapshot
    out_path = RAW_DIR / f"{today}.json"
    out_path.write_text(json.dumps(snapshot, indent=2))
    logger.info(f"Saved {len(snapshot)} players → {out_path}")

    # 4. Upsert to SQLite
    logger.info("Writing to SQLite…")
    n_players, n_accounts = upsert_players(snapshot, today)
    logger.info(f"  {n_players:,} players upserted | {n_accounts:,} account rows inserted")

    # 5. Quick stats
    tier_counts: dict = {}
    for p in snapshot:
        for acct in p.get("accounts", []):
            t = acct.get("tier", "Unranked")
            tier_counts[t] = tier_counts.get(t, 0) + 1
    logger.info("Rank distribution: " + " | ".join(
        f"{t}:{n}" for t, n in sorted(tier_counts.items(),
        key=lambda x: TIER_ORDER.index(x[0]) if x[0] in TIER_ORDER else -1,
        reverse=True)
    ))

    logger.info("TTP scraper complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--full-accounts",
        action="store_true",
        help="Fetch individual player pages to capture all soloq accounts per server",
    )
    args = parser.parse_args()
    main(full_accounts=args.full_accounts)
