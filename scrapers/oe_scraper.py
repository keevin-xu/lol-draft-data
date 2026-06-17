"""
Oracle's Elixir scraper — downloads annual match CSVs from Oracle's Elixir
public Google Drive folder, filters to T2 leagues, and upserts match rows
into SQLite.

Data is sourced from the public Google Drive folder maintained by OE:
  https://drive.google.com/drive/folders/1gLSw0RLjBbtaNy0dgnGQDAZOHIgCe-HH

The datalisk.io API is used to check remote updatedAt timestamps so we only
re-download the current year's file when OE has actually published new data.

Run:  python scrapers/oe_scraper.py
"""

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
from loguru import logger
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).parent.parent
DB_PATH = _ROOT / "db" / "lol_model.db"
RAW_DIR = _ROOT / "data" / "raw" / "oracleselixir"
GDRIVE_IDS_PATH = RAW_DIR / "gdrive_ids.json"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
YEARS = [2024, 2025, 2026]
CURRENT_YEAR = 2026
REFRESH_DAYS = 7  # re-download current-year CSV when OE has newer data AND local is older than this

# Google Drive file IDs for each year's OE match data CSV.
# Google Drive keeps the same ID even when file content is updated.
# If IDs become stale, delete gdrive_ids.json and re-run to auto-discover.
_FALLBACK_IDS = {
    2024: "1IjIEhLc9n8eLKeY-yh_YigKVWbhgGBsN",
    2025: "1v6LRphp2kYciU4SXp0PCjEMuev1bDejc",
    2026: "1hnpbrUpBMS1TZI7IovfpKeZfWJH1Aptm",
}
GDRIVE_FOLDER_ID = "1gLSw0RLjBbtaNy0dgnGQDAZOHIgCe-HH"

# Datalisk API — public API key embedded in the OE website JS bundle.
# Used only for reading metadata (updatedAt timestamps), not for downloads.
DATALISK_URL = "https://oe.datalisk.io/matchData"
DATALISK_API_KEY = "f561197a-82ea-4e54-acd2-386979018a7a"

T2_LEAGUES = {
    # North America
    "NACL",
    # Korea — OE uses "LCKC" for LCK Challengers
    "LCKC",
    # Europe / EMEA — OE uses "EM" for EMEA Masters, "LEC" for the old ERL umbrella
    "LEC",
    "EM",            # EMEA Masters (cross-regional)
    "NLC",           # Nordic & Baltic
    "LFL",           # France
    "ESLOL",         # Italy
    "LVP SL",        # Spain (was "PG.Nationals" before 2024 rebrand)
    "TCL",           # Turkey
    # Oceania / Pacific
    "LCO",
    # Latin America — OE uses "LTA N" / "LTA S" after 2024 rebrand of LLA
    "LLA",           # pre-2025 name
    "LTA N",         # Latin America North (2025+)
    "LTA S",         # Latin America South (2025+)
    # Brazil — OE uses "LRN" / "LRS" for CBLOL Academy circuits
    "CBLOL Academy",
    "LRN",           # Brazil regional T2 (north)
    "LRS",           # Brazil regional T2 (south)
    # Southeast Asia
    "PCS",           # Taiwan / HK / Macao
    "VCS",           # Vietnam
    # Japan
    "LJL",
}


# ---------------------------------------------------------------------------
# HTTP session with retries + exponential backoff
# ---------------------------------------------------------------------------
def _make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[500, 502, 503, 504],
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


# ---------------------------------------------------------------------------
# Google Drive file ID management
# ---------------------------------------------------------------------------
def load_gdrive_ids() -> dict:
    """Load cached file IDs from disk, falling back to hardcoded defaults."""
    if GDRIVE_IDS_PATH.exists():
        try:
            stored = json.loads(GDRIVE_IDS_PATH.read_text())
            merged = dict(_FALLBACK_IDS)
            merged.update({int(k): v for k, v in stored.items()})
            return merged
        except Exception:
            pass
    return dict(_FALLBACK_IDS)


def save_gdrive_ids(ids: dict) -> None:
    GDRIVE_IDS_PATH.write_text(json.dumps({str(k): v for k, v in ids.items()}, indent=2))


def discover_gdrive_ids(session: Optional[requests.Session] = None) -> dict:
    """
    Scrape the OE public Google Drive folder to discover file IDs for all years.
    Requires Playwright (used only when IDs are missing or stale).
    """
    logger.info("Discovering Google Drive file IDs via Playwright…")
    try:
        import asyncio
        from playwright.async_api import async_playwright

        async def _scrape():
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                url = f"https://drive.google.com/drive/folders/{GDRIVE_FOLDER_ID}"
                try:
                    await page.goto(url, wait_until="networkidle", timeout=30000)
                except Exception:
                    pass
                await page.wait_for_timeout(4000)
                items = await page.eval_on_selector_all(
                    "[data-id]",
                    "els => els.map(e => ({id: e.getAttribute('data-id'), text: e.textContent.trim()}))",
                )
                await browser.close()
                return items

        items = asyncio.run(_scrape())
        ids = {}
        for item in items:
            text = item.get("text", "")
            file_id = item.get("id", "")
            if not file_id or len(file_id) < 20:
                continue
            for year in range(2014, CURRENT_YEAR + 1):
                if str(year) in text and "LoL" in text:
                    ids[year] = file_id
                    break
        if ids:
            logger.info(f"Discovered {len(ids)} file IDs from Google Drive")
            save_gdrive_ids(ids)
        return ids
    except Exception as e:
        logger.warning(f"Drive discovery failed ({e}), using fallback IDs")
        return dict(_FALLBACK_IDS)


# ---------------------------------------------------------------------------
# Remote metadata check via datalisk
# ---------------------------------------------------------------------------
def fetch_remote_metadata(session: Optional[requests.Session] = None) -> dict:
    """
    Return {year: updated_at_timestamp} from the datalisk API.
    Returns an empty dict on failure (scraper will proceed without freshness check).
    """
    s = session or _make_session()
    try:
        r = s.get(
            DATALISK_URL,
            headers={
                "x-api-key": DATALISK_API_KEY,
                "referer": "https://oracleselixir.com/",
                "user-agent": "Mozilla/5.0",
            },
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        return {
            entry["year"]: entry.get("updatedAt", "")
            for entry in data
            if "year" in entry
        }
    except Exception as e:
        logger.warning(f"Could not fetch datalisk metadata: {e}")
        return {}


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------
def _gdrive_url(file_id: str) -> str:
    return f"https://drive.google.com/uc?export=download&id={file_id}"


def _remote_is_newer(year: int, remote_ts: str, local_path: Path) -> bool:
    """True if the remote file was updated after the local file was written."""
    if not local_path.exists() or not remote_ts:
        return True
    try:
        remote_dt = datetime.fromisoformat(remote_ts.replace("Z", "+00:00"))
        local_mtime = datetime.fromtimestamp(local_path.stat().st_mtime, tz=timezone.utc)
        return remote_dt > local_mtime
    except Exception:
        return True


def download_year(
    year: int,
    gdrive_ids: Optional[dict] = None,
    remote_meta: Optional[dict] = None,
    session: Optional[requests.Session] = None,
) -> Path:
    """
    Download the OE CSV for *year* from Google Drive and save to RAW_DIR.

    Caching rules:
      - Historical years (< CURRENT_YEAR): cached forever once present.
      - Current year: re-downloaded when the local copy is older than
        REFRESH_DAYS and OE has published a newer version.
    """
    out_path = RAW_DIR / f"{year}.csv"
    ids = gdrive_ids or load_gdrive_ids()
    meta = remote_meta or {}

    if out_path.exists():
        if year < CURRENT_YEAR:
            logger.info(f"{year}.csv cached — skipping download")
            return out_path

        age_days = (time.time() - out_path.stat().st_mtime) / 86400
        remote_ts = meta.get(year, "")
        newer = _remote_is_newer(year, remote_ts, out_path)

        if not newer:
            logger.info(f"{year}.csv is already up-to-date ({remote_ts})")
            return out_path
        if age_days < REFRESH_DAYS:
            logger.info(
                f"{year}.csv is {age_days:.1f}d old and OE has newer data "
                f"({remote_ts}), but skipping — refresh window is {REFRESH_DAYS}d"
            )
            return out_path
        logger.info(f"{year}.csv is stale ({age_days:.1f}d) — refreshing")

    file_id = ids.get(year)
    if not file_id:
        logger.warning(f"No Google Drive ID for {year} — attempting discovery")
        ids = discover_gdrive_ids(session)
        file_id = ids.get(year)
        if not file_id:
            raise RuntimeError(f"Cannot find Google Drive file ID for {year}")

    url = _gdrive_url(file_id)
    logger.info(f"Downloading {year} from Google Drive (id={file_id[:10]}…)")
    s = session or _make_session()
    r = s.get(url, timeout=180, stream=True)
    r.raise_for_status()

    total = 0
    with open(out_path, "wb") as fh:
        for chunk in r.iter_content(chunk_size=1024 * 256):
            fh.write(chunk)
            total += len(chunk)

    logger.info(f"Saved {total / 1e6:.1f} MB → {out_path}")
    return out_path


# ---------------------------------------------------------------------------
# Loading helpers (also imported by model code)
# ---------------------------------------------------------------------------
def _load_raw(years: list) -> pd.DataFrame:
    """Read CSVs for *years*, concatenate, and filter to T2_LEAGUES."""
    frames = []
    for year in years:
        path = RAW_DIR / f"{year}.csv"
        if not path.exists():
            raise FileNotFoundError(
                f"Missing {path}. Run download_year({year}) first."
            )
        df = pd.read_csv(path, low_memory=False)
        df = df[df["league"].isin(T2_LEAGUES)]
        frames.append(df)
    return pd.concat(frames, ignore_index=True)


def load_player_rows(years: list = None) -> pd.DataFrame:
    """One row per player per game (position != 'team')."""
    years = years or YEARS
    df = _load_raw(years)
    return df[df["position"] != "team"].reset_index(drop=True)


def load_team_rows(years: list = None) -> pd.DataFrame:
    """One row per team per game (position == 'team')."""
    years = years or YEARS
    df = _load_raw(years)
    return df[df["position"] == "team"].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Match-row builder
# ---------------------------------------------------------------------------
def build_match_rows(team_df: pd.DataFrame) -> pd.DataFrame:
    """
    Collapse the 2 team-summary rows per game into 1 match row.
    Games with missing Blue or Red side data are dropped.
    """
    records = []
    for gameid, grp in team_df.groupby("gameid"):
        blue = grp[grp["side"] == "Blue"]
        red = grp[grp["side"] == "Red"]
        if blue.empty or red.empty:
            continue
        b, r = blue.iloc[0], red.iloc[0]

        try:
            result_val = int(b.get("result", 0))
        except (ValueError, TypeError):
            result_val = 0

        try:
            gamelength = int(b.get("gamelength", 0))
        except (ValueError, TypeError):
            gamelength = 0

        records.append({
            "gameid":     str(gameid),
            "date":       str(b.get("date", "")),
            "league":     str(b.get("league", "")),
            "patch":      str(b.get("patch", "")),
            "playoffs":   int(b.get("playoffs", 0)),
            "blue_team":  str(b.get("teamname", "")),
            "red_team":   str(r.get("teamname", "")),
            "winner":     "blue" if result_val == 1 else "red",
            "gamelength": gamelength,
        })

    return pd.DataFrame(records)


# ---------------------------------------------------------------------------
# SQLite upsert
# ---------------------------------------------------------------------------
def upsert_matches(matches_df: pd.DataFrame) -> int:
    """
    INSERT OR IGNORE each match row into SQLite.
    Returns the count of newly inserted rows.
    """
    conn = sqlite3.connect(DB_PATH)
    inserted = 0
    try:
        for _, row in matches_df.iterrows():
            conn.execute(
                """
                INSERT OR IGNORE INTO matches
                    (gameid, date, league, patch, playoffs,
                     blue_team, red_team, winner, gamelength)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["gameid"],
                    row["date"],
                    row["league"],
                    row["patch"],
                    row["playoffs"],
                    row["blue_team"],
                    row["red_team"],
                    row["winner"],
                    row["gamelength"],
                ),
            )
            inserted += conn.execute("SELECT changes()").fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    return inserted


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    session = _make_session()
    gdrive_ids = load_gdrive_ids()
    remote_meta = fetch_remote_metadata(session)

    # 1. Download CSVs
    for year in YEARS:
        download_year(year, gdrive_ids=gdrive_ids, remote_meta=remote_meta, session=session)

    # 2. Load player rows (informational log only)
    logger.info("Loading player rows…")
    player_df = load_player_rows(YEARS)
    n_games = player_df["gameid"].nunique()
    logger.info(
        f"  {len(player_df):,} T2 player-game rows | {n_games:,} unique games"
    )

    # 3. Build one match row per game from team-summary rows
    logger.info("Building match rows from team-summary data…")
    team_df = load_team_rows(YEARS)
    matches_df = build_match_rows(team_df)
    logger.info(f"  {len(matches_df):,} match rows built")

    # 4. Upsert into SQLite
    logger.info("Writing to SQLite…")
    n_new = upsert_matches(matches_df)
    skipped = len(matches_df) - n_new
    logger.info(f"  {n_new:,} new matches inserted | {skipped:,} already present")

    logger.info("OE scraper complete.")


if __name__ == "__main__":
    main()
