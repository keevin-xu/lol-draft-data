"""
Match / draft data scraper — pulls Tier-2 LoL pro match draft data from
Oracle's Elixir annual CSVs and loads it into a dedicated SQLite DB
(`db/match_data.db`).

What it captures (the last ~5 years of T2 play, 2021-2026):
  * every champion each player drafted, with their role  → draft_picks
  * full pick/ban draft order per team                   → team_pickbans
  * per-game metadata + the patch the game was played on → games
  * a derived per-patch summary                          → patches

Oracle's Elixir already contains all of this in one CSV per year; the existing
`oe_scraper.py` downloads the same files but only persists collapsed team-level
match rows. This scraper persists the player-level draft data that one discards.

Patch handling: patch is a per-game attribute, stored once on `games`. Draft
rows reference the game via `gameid`, so join draft_picks.gameid -> games.gameid
to get a pick's patch (no patch duplication on every row).

Data is sourced from the public OE Google Drive folder:
  https://drive.google.com/drive/folders/1gLSw0RLjBbtaNy0dgnGQDAZOHIgCe-HH
Raw CSVs are shared with oe_scraper.py under data/raw/oracleselixir/, so years
already downloaded are not fetched again.

Run:
  python scrapers/match_data_scraper.py                 # all years (2021-2026)
  python scrapers/match_data_scraper.py --years 2025 2026
  python scrapers/match_data_scraper.py --no-download   # use cached CSVs only
"""

import argparse
import math
import re
import sqlite3
from pathlib import Path
from typing import Any, List, Optional

import pandas as pd
import requests
from loguru import logger

# Reuse the T2 league filter, retry session, and Drive URL helper from the
# sibling OE scraper (scrapers/ is sys.path[0] when run as a script).
from oe_scraper import T2_LEAGUES, _gdrive_url, _make_session

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).parent.parent
DB_PATH = _ROOT / "db" / "match_data.db"
SCHEMA_PATH = _ROOT / "db" / "match_data_schema.sql"
RAW_DIR = _ROOT / "data" / "raw" / "oracleselixir"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
YEARS = [2021, 2022, 2023, 2024, 2025, 2026]
CURRENT_YEAR = 2026

# Google Drive file IDs per year (Drive keeps the same ID across content
# updates). Discovered from the public OE folder; 2024-2026 match oe_scraper.
GDRIVE_IDS = {
    2021: "1fzwTTz77hcnYjOnO9ONeoPrkWCoOSecA",
    2022: "1EHmptHyzY8owv0BAcNKtkQpMwfkURwRy",
    2023: "1XXk2LO0CsNADBB1LRGOV5rUpyZdEZ8s2",
    2024: "1IjIEhLc9n8eLKeY-yh_YigKVWbhgGBsN",
    2025: "1v6LRphp2kYciU4SXp0PCjEMuev1bDejc",
    2026: "1hnpbrUpBMS1TZI7IovfpKeZfWJH1Aptm",
}

# Only the columns we need (the full CSV has ~165) — keeps memory low.
USECOLS = [
    "gameid", "datacompleteness", "league", "year", "split", "playoffs",
    "date", "patch", "side", "position", "playername", "playerid",
    "teamname", "firstPick", "champion",
    "ban1", "ban2", "ban3", "ban4", "ban5",
    "pick1", "pick2", "pick3", "pick4", "pick5",
    "gamelength", "result",
]
_BANS = ["ban1", "ban2", "ban3", "ban4", "ban5"]
_PICKS = ["pick1", "pick2", "pick3", "pick4", "pick5"]


# ---------------------------------------------------------------------------
# NaN-safe cell coercion (pandas reads blanks as NaN)
# ---------------------------------------------------------------------------
def _s(v: Any) -> Optional[str]:
    """Cell -> trimmed string, or None if blank/NaN."""
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    s = str(v).strip()
    return s or None


def _i(v: Any) -> Optional[int]:
    """Cell -> int, or None if blank/NaN/non-numeric."""
    s = _s(v)
    if s is None:
        return None
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Download (Google Drive, with large-file confirm-token handling)
# ---------------------------------------------------------------------------
def download_year(year: int, session: Optional[requests.Session] = None) -> Path:
    """
    Download the OE CSV for *year* into RAW_DIR, sharing the cache with
    oe_scraper.py. Historical years are cached forever; the current year is
    always re-downloaded so in-season corrections are picked up.
    """
    out_path = RAW_DIR / f"{year}.csv"
    if out_path.exists() and year < CURRENT_YEAR:
        logger.info(f"{year}.csv cached — skipping download")
        return out_path

    file_id = GDRIVE_IDS.get(year)
    if not file_id:
        raise RuntimeError(f"No Google Drive file ID configured for {year}")

    s = session or _make_session()
    logger.info(f"Downloading {year} from Google Drive (id={file_id[:10]}…)")
    r = s.get(_gdrive_url(file_id), timeout=180, stream=True)
    r.raise_for_status()

    # Large files may return an HTML interstitial with a confirm token instead
    # of the CSV. Detect it from the first chunk and retry via the confirm URL.
    chunks = r.iter_content(chunk_size=1024 * 256)
    first = next(chunks, b"")
    if first[:512].lstrip().lower().startswith(b"<!doctype html") or b"<html" in first[:512].lower():
        html = first.decode("utf-8", "ignore")
        token = re.search(r"confirm=([0-9A-Za-z_-]+)", html)
        uuid = re.search(r'name="uuid"\s+value="([0-9A-Za-z_-]+)"', html)
        confirm_url = (
            "https://drive.usercontent.google.com/download"
            f"?id={file_id}&export=download&confirm="
            f"{token.group(1) if token else 't'}"
            + (f"&uuid={uuid.group(1)}" if uuid else "")
        )
        logger.info("Drive returned a confirm page — retrying via usercontent URL")
        r = s.get(confirm_url, timeout=300, stream=True)
        r.raise_for_status()
        chunks = r.iter_content(chunk_size=1024 * 256)
        first = next(chunks, b"")

    total = 0
    with open(out_path, "wb") as fh:
        if first:
            fh.write(first)
            total += len(first)
        for chunk in chunks:
            fh.write(chunk)
            total += len(chunk)

    logger.info(f"Saved {total / 1e6:.1f} MB → {out_path}")
    return out_path


# ---------------------------------------------------------------------------
# Load + filter
# ---------------------------------------------------------------------------
def load_t2_frame(years: List[int]) -> pd.DataFrame:
    """Read the needed columns for *years*, concatenate, and filter to T2."""
    frames = []
    for year in years:
        path = RAW_DIR / f"{year}.csv"
        if not path.exists():
            raise FileNotFoundError(f"Missing {path}. Run download_year({year}) first.")
        df = pd.read_csv(path, usecols=lambda c: c in USECOLS, low_memory=False)
        df = df[df["league"].isin(T2_LEAGUES)]
        frames.append(df)
        logger.info(f"  {year}: {len(df):,} T2 rows")
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------
def build_games(team_df: pd.DataFrame) -> List[tuple]:
    """One games row per game from the two team-summary rows."""
    rows = []
    for gameid, grp in team_df.groupby("gameid"):
        blue = grp[grp["side"] == "Blue"]
        red = grp[grp["side"] == "Red"]
        if blue.empty or red.empty:
            continue
        b, r = blue.iloc[0], red.iloc[0]
        winner = "blue" if _i(b.get("result")) == 1 else "red"
        rows.append((
            _s(gameid), _s(b.get("date")), _s(b.get("league")), _i(b.get("year")),
            _s(b.get("split")), _i(b.get("playoffs")), _s(b.get("patch")),
            _s(b.get("teamname")), _s(r.get("teamname")), winner,
            _i(b.get("gamelength")), _s(b.get("datacompleteness")),
        ))
    return rows


def build_draft_picks(player_df: pd.DataFrame) -> List[tuple]:
    """One draft_picks row per player per game (champion + role)."""
    rows = []
    for t in player_df.itertuples(index=False):
        d = t._asdict()
        rows.append((
            _s(d.get("gameid")), _s(d.get("side")), _s(d.get("position")),
            _s(d.get("playername")), _s(d.get("playerid")), _s(d.get("teamname")),
            _s(d.get("champion")), _i(d.get("result")),
        ))
    return rows


def build_team_pickbans(team_df: pd.DataFrame) -> List[tuple]:
    """One team_pickbans row per team per game (full draft order)."""
    rows = []
    for t in team_df.itertuples(index=False):
        d = t._asdict()
        rows.append((
            _s(d.get("gameid")), _s(d.get("side")), _s(d.get("teamname")),
            _i(d.get("firstPick")),
            *[_s(d.get(c)) for c in _BANS],
            *[_s(d.get(c)) for c in _PICKS],
        ))
    return rows


# ---------------------------------------------------------------------------
# DB write
# ---------------------------------------------------------------------------
def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())


def upsert_all(
    games: List[tuple],
    picks: List[tuple],
    pickbans: List[tuple],
) -> dict:
    """Idempotent upsert of all three tables, then rebuild the patches summary."""
    conn = sqlite3.connect(DB_PATH, timeout=60)
    try:
        ensure_schema(conn)

        conn.executemany(
            """
            INSERT INTO games
                (gameid, date, league, year, split, playoffs, patch,
                 blue_team, red_team, winner, gamelength, datacompleteness)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(gameid) DO UPDATE SET
                date=excluded.date, league=excluded.league, year=excluded.year,
                split=excluded.split, playoffs=excluded.playoffs,
                patch=excluded.patch, blue_team=excluded.blue_team,
                red_team=excluded.red_team, winner=excluded.winner,
                gamelength=excluded.gamelength,
                datacompleteness=excluded.datacompleteness
            """,
            games,
        )
        conn.executemany(
            """
            INSERT INTO draft_picks
                (gameid, side, position, playername, playerid, teamname,
                 champion, result)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(gameid, side, position) DO UPDATE SET
                playername=excluded.playername, playerid=excluded.playerid,
                teamname=excluded.teamname, champion=excluded.champion,
                result=excluded.result
            """,
            picks,
        )
        conn.executemany(
            """
            INSERT INTO team_pickbans
                (gameid, side, teamname, first_pick,
                 ban1, ban2, ban3, ban4, ban5,
                 pick1, pick2, pick3, pick4, pick5)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(gameid, side) DO UPDATE SET
                teamname=excluded.teamname, first_pick=excluded.first_pick,
                ban1=excluded.ban1, ban2=excluded.ban2, ban3=excluded.ban3,
                ban4=excluded.ban4, ban5=excluded.ban5,
                pick1=excluded.pick1, pick2=excluded.pick2, pick3=excluded.pick3,
                pick4=excluded.pick4, pick5=excluded.pick5
            """,
            pickbans,
        )

        # Rebuild the derived per-patch summary from the games table.
        conn.execute("DELETE FROM patches")
        conn.execute(
            """
            INSERT INTO patches (patch, num_games, first_date, last_date)
            SELECT patch, COUNT(*), MIN(date), MAX(date)
            FROM games
            WHERE patch IS NOT NULL AND patch != ''
            GROUP BY patch
            """
        )

        counts = {
            "games": conn.execute("SELECT COUNT(*) FROM games").fetchone()[0],
            "draft_picks": conn.execute("SELECT COUNT(*) FROM draft_picks").fetchone()[0],
            "team_pickbans": conn.execute("SELECT COUNT(*) FROM team_pickbans").fetchone()[0],
            "patches": conn.execute("SELECT COUNT(*) FROM patches").fetchone()[0],
        }
        conn.commit()
        return counts
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main(years: Optional[List[int]] = None, download: bool = True) -> None:
    years = years or YEARS
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    if download:
        session = _make_session()
        for year in years:
            download_year(year, session=session)

    logger.info(f"Loading + filtering T2 rows for {years}…")
    df = load_t2_frame(years)

    team_df = df[df["position"] == "team"].reset_index(drop=True)
    player_df = df[df["position"] != "team"].reset_index(drop=True)
    logger.info(
        f"  {team_df['gameid'].nunique():,} games | "
        f"{len(player_df):,} player-game rows | {len(team_df):,} team-game rows"
    )

    logger.info("Building rows…")
    games = build_games(team_df)
    picks = build_draft_picks(player_df)
    pickbans = build_team_pickbans(team_df)
    logger.info(
        f"  {len(games):,} games | {len(picks):,} draft picks | "
        f"{len(pickbans):,} team pick/ban rows"
    )

    logger.info(f"Writing to {DB_PATH.name}…")
    counts = upsert_all(games, picks, pickbans)
    logger.info(
        "Done. Table totals: "
        f"{counts['games']:,} games | {counts['draft_picks']:,} draft_picks | "
        f"{counts['team_pickbans']:,} team_pickbans | {counts['patches']:,} patches"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", type=int, nargs="+", default=None,
                        help=f"Years to scrape (default: {YEARS})")
    parser.add_argument("--no-download", action="store_true",
                        help="Use cached CSVs in data/raw/oracleselixir/ only")
    args = parser.parse_args()
    main(years=args.years, download=not args.no_download)
