# Scraper Roadmap — Today's Sprint
*Goal: working scrapers for soloq data, pro match data, and day-of rosters*

---

## Overview

Three scrapers to build today, in recommended order:

1. **`oe_scraper.py`** — Oracle's Elixir pro match CSV (easiest, no browser)
2. **`ttp_scraper.py`** — TrackingThePros soloq data (harder, needs Playwright)
3. **`roster_scraper.py`** — Current team rosters via Leaguepedia (medium)

---

## Scraper 1: Oracle's Elixir (`oe_scraper.py`)

### How the source works

Oracle's Elixir hosts annual match CSVs on a public S3 bucket. No authentication needed.

```
https://oracleselixir-downloadable-match-data.s3-us-west-2.amazonaws.com/{YEAR}_LoL_esports_match_data_from_OraclesElixir.csv
```

Confirmed working years: 2025, 2026. Test 2024 and 2023 too.

### CSV structure

Each row is one **player in one game**. A standard 5v5 game = 10 player rows + 2 team-summary rows (12 rows per game total). The team-summary rows have `position == "team"`.

Key columns:
```
gameid          unique game identifier
date            ISO datetime
league          league name (e.g. "NACL", "LCK CL")
split           Spring/Summer
year            int
patch           e.g. "14.10"
playoffs        0 or 1
side            Blue / Red
position        top/jng/mid/bot/sup/team
playername      player handle (blank on team rows)
teamname        team name
result          1 = win, 0 = loss
gamelength      seconds
kills/deaths/assists
totalgold
```

### T2 League filter

Include only these `league` values (update this list as we add regions):
```python
T2_LEAGUES = {
    # North America
    "NACL",
    # Korea
    "LCK CL",
    # Europe / EMEA
    "LEC",          # only until 2023; replaced by EMEA
    "EMEA Masters", # cross-regional T2 tournament
    "NLC",          # Nordic & Baltic
    "LFL",          # France
    "ESLOL",        # Italy
    "PG.Nationals", # Spain
    "TCL",          # Turkey
    "LCO",          # Oceania (Pacific)
    # Latin America
    "LLA",
    # Brazil
    "CBLOL Academy",
    # Southeast Asia
    "PCS",          # Taiwan/HK/Macao
    "VCS",          # Vietnam
    # Japan
    "LJL",
}
```

### Implementation plan

```python
# scrapers/oe_scraper.py

import requests
import pandas as pd
from pathlib import Path
from loguru import logger
from datetime import date

RAW_DIR = Path("data/raw/oracleselixir")
S3_BASE = "https://oracleselixir-downloadable-match-data.s3-us-west-2.amazonaws.com"

T2_LEAGUES = { ... }  # full set above

def download_year(year: int) -> Path:
    """Download the full year CSV and save to data/raw/oracleselixir/."""
    url = f"{S3_BASE}/{year}_LoL_esports_match_data_from_OraclesElixir.csv"
    out_path = RAW_DIR / f"{year}.csv"
    if out_path.exists():
        logger.info(f"{year}.csv already exists, skipping download")
        return out_path
    logger.info(f"Downloading {year} match data...")
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    out_path.write_bytes(r.content)
    logger.info(f"Saved {len(r.content) / 1e6:.1f} MB → {out_path}")
    return out_path

def load_t2_matches(years: list[int]) -> pd.DataFrame:
    """Load and filter to T2 leagues only, player rows only."""
    frames = []
    for year in years:
        path = RAW_DIR / f"{year}.csv"
        if not path.exists():
            download_year(year)
        df = pd.read_csv(path, low_memory=False)
        df = df[df["league"].isin(T2_LEAGUES)]
        df = df[df["position"] != "team"]   # drop team-summary rows
        frames.append(df)
    return pd.concat(frames, ignore_index=True)

def load_t2_team_results(years: list[int]) -> pd.DataFrame:
    """Return one row per team per game (team-summary rows only)."""
    frames = []
    for year in years:
        path = RAW_DIR / f"{year}.csv"
        if not path.exists():
            download_year(year)
        df = pd.read_csv(path, low_memory=False)
        df = df[df["league"].isin(T2_LEAGUES)]
        df = df[df["position"] == "team"]
        frames.append(df)
    return pd.concat(frames, ignore_index=True)

if __name__ == "__main__":
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    for year in [2024, 2025, 2026]:
        download_year(year)
    df = load_t2_matches([2024, 2025, 2026])
    logger.info(f"Loaded {len(df):,} T2 player-game rows across {df['gameid'].nunique():,} games")
```

### Expected output

After running: `data/raw/oracleselixir/2024.csv`, `2025.csv`, `2026.csv`  
Log should show something like: `Loaded 180,000+ T2 player-game rows`

---

## Scraper 2: TrackingThePros (`ttp_scraper.py`)

### How the source works

`https://www.trackingthepros.com/players` renders a React table via JavaScript. The page HTML is empty on load — data comes from an internal XHR after the page mounts. We need Playwright to:
1. Navigate to the page
2. Wait for the table to populate
3. Extract all rows

### Finding the internal API (attempt first)

Before committing to full Playwright scraping, intercept the network requests to see if TTP fires a JSON API call we can hit directly:

```python
# Debug script — run once to find the API endpoint
import asyncio
from playwright.async_api import async_playwright

async def find_api():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()

        api_calls = []
        page.on("request", lambda r: api_calls.append(r.url) if "json" in r.url or "/api/" in r.url or "/players" in r.url else None)

        await page.goto("https://www.trackingthepros.com/players")
        await page.wait_for_timeout(5000)  # wait 5s for all XHR

        print("XHR calls made:")
        for url in api_calls:
            print(url)

        await browser.close()

asyncio.run(find_api())
```

**If a JSON endpoint is found:** hit it directly with `requests` — much faster and more stable than DOM scraping.

**If no JSON endpoint:** fall back to DOM extraction below.

### DOM extraction plan (fallback)

```python
# scrapers/ttp_scraper.py

import asyncio
import json
from datetime import date
from pathlib import Path
from playwright.async_api import async_playwright
from loguru import logger

RAW_DIR = Path("data/raw/trackingthepros")

async def scrape_players() -> list[dict]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        logger.info("Navigating to TrackingThePros...")
        await page.goto("https://www.trackingthepros.com/players", wait_until="networkidle")

        # Wait for the table to have at least one data row
        await page.wait_for_selector("table tbody tr", timeout=15000)

        rows = await page.query_selector_all("table tbody tr")
        logger.info(f"Found {len(rows)} player rows")

        players = []
        for row in rows:
            cells = await row.query_selector_all("td")
            if len(cells) < 6:
                continue

            player = {
                "player_name": await cells[0].inner_text(),
                "role":        await cells[1].inner_text(),
                "team":        await cells[2].inner_text(),
                "region":      await cells[3].inner_text(),
                "highest_rank": await cells[4].inner_text(),
                "accounts_raw": await cells[5].inner_text(),
                # "in_game" column is volatile, skip
            }
            players.append(player)

        await browser.close()
        return players

def parse_rank(rank_str: str) -> dict:
    """
    Parse a rank string like 'Challenger 1,423 LP' or 'Diamond I 72 LP'
    into structured fields.
    """
    # Implementation: regex match tier + division + LP
    import re
    tier_order = ["Iron","Bronze","Silver","Gold","Platinum","Emerald","Diamond","Master","Grandmaster","Challenger"]
    # ... parse logic here
    return {"tier": ..., "division": ..., "lp": ...}

async def main():
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    players = await scrape_players()

    out_path = RAW_DIR / f"{date.today()}.json"
    with open(out_path, "w") as f:
        json.dump(players, f, indent=2)
    logger.info(f"Saved {len(players)} players → {out_path}")

if __name__ == "__main__":
    asyncio.run(main())
```

### Expected output

```json
[
  {
    "player_name": "Faker",
    "role": "Mid",
    "team": "T1",
    "region": "KR",
    "highest_rank": "Challenger 1800 LP",
    "accounts_raw": "hide on bush (KR), SKT T1 Faker (KR)"
  },
  ...
]
```

### Parsing accounts

The `accounts_raw` field will contain comma-separated summoner names. Parse each into:
```python
{
  "summoner_name": "hide on bush",
  "server": "KR",
  "rank": "Challenger",
  "lp": 1800
}
```

The site sometimes shows rank next to each account. Capture it if present; fall back to `highest_rank` if not.

### Rank → Numeric Rating

```python
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

import math

def rank_to_rating(tier: str, lp: int) -> float:
    base = TIER_BASE[tier]
    if tier in ("Master", "Grandmaster", "Challenger"):
        # Log compression for high ELO
        return base + 400 * math.log(1 + lp / 400)
    else:
        return base + lp
```

---

## Scraper 3: Roster Scraper (`roster_scraper.py`)

### Source: Leaguepedia (MediaWiki API)

Leaguepedia exposes a public Cargo API — no authentication required.

```
https://lol.fandom.com/api.php?action=cargoquery&...
```

The most useful tables:
- `ScoreboardPlayers` — player in each game with team, role, result
- `TournamentRosters` — official registered roster per team per tournament

### Query for current active rosters

```python
import requests

def get_current_rosters(leagues: list[str]) -> list[dict]:
    """
    Pull active rosters from Leaguepedia for the given leagues.
    Uses the TournamentRosters Cargo table.
    """
    base = "https://lol.fandom.com/api.php"
    params = {
        "action": "cargoquery",
        "tables": "TournamentRosters=TR,Players=P",
        "join_on": "TR.RosterPage=P.OverviewPage",
        "fields": "TR.Team,TR.Role,P.ID,P.Residency,TR.Tournament",
        "where": f"TR.Tournament IN ({','.join(repr(l) for l in leagues)})",
        "limit": 500,
        "format": "json",
    }
    r = requests.get(base, params=params, timeout=30)
    r.raise_for_status()
    return r.json()["cargoquery"]
```

### T2 tournament names on Leaguepedia (2026 examples)

```python
T2_TOURNAMENTS_2026 = [
    "NACL 2026 Spring",
    "NACL 2026 Summer",
    "LCK CL 2026 Spring",
    "LCK CL 2026 Summer",
    # Add ERLs, LLA, CBLOL Academy, etc.
]
```

**Important:** Leaguepedia tournament names change every split. You'll need to discover the exact names at the start of each split. Query approach:

```python
# Find all T2 tournaments in 2026
params = {
    "action": "cargoquery",
    "tables": "Tournaments",
    "fields": "Name,Region,DateStart",
    "where": "DateStart > '2026-01-01' AND Tier = '2'",
    "limit": 100,
    "format": "json",
}
```

### Mapping TTP players to Leaguepedia

The player `ID` on Leaguepedia should match `player_name` from TTP (both use the in-game handle). But casing and special characters may differ. Build a fuzzy match:

```python
from difflib import get_close_matches

def match_player(ttp_name: str, lp_names: list[str]) -> str | None:
    matches = get_close_matches(ttp_name.lower(), [n.lower() for n in lp_names], n=1, cutoff=0.85)
    return matches[0] if matches else None
```

Store any unmatched names in `data/processed/unmatched_players.json` for manual review.

---

## Database Schema

```sql
-- db/schema.sql

CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY,
    player_name TEXT NOT NULL UNIQUE,
    role TEXT,
    team TEXT,
    region TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    player_id INTEGER REFERENCES players(id),
    summoner_name TEXT,
    server TEXT,
    rank_tier TEXT,
    lp INTEGER,
    soloq_rating REAL,
    snapshot_date TEXT
);

CREATE TABLE IF NOT EXISTS teams (
    id INTEGER PRIMARY KEY,
    team_name TEXT NOT NULL UNIQUE,
    region TEXT,
    league TEXT,
    pro_elo REAL DEFAULT 1500.0,
    games_played INTEGER DEFAULT 0,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY,
    gameid TEXT UNIQUE,
    date TEXT,
    league TEXT,
    patch TEXT,
    playoffs INTEGER,
    blue_team TEXT,
    red_team TEXT,
    winner TEXT,   -- 'blue' or 'red'
    gamelength INTEGER
);

CREATE TABLE IF NOT EXISTS rosters (
    id INTEGER PRIMARY KEY,
    team TEXT,
    player_name TEXT,
    role TEXT,
    snapshot_date TEXT,
    tournament TEXT
);
```

---

## Running Order Today

```bash
# 1. Install dependencies
pip install requests pandas playwright loguru --break-system-packages
playwright install chromium

# 2. Init DB
python db/init_db.py

# 3. Download OE match CSVs (fast, ~30 seconds)
python scrapers/oe_scraper.py

# 4. Scrape TTP player/soloq data (slow, ~2-3 minutes)
python scrapers/ttp_scraper.py

# 5. Pull today's rosters
python scrapers/roster_scraper.py

# 6. Verify
python -c "
import sqlite3, pandas as pd
conn = sqlite3.connect('db/lol_model.db')
print('Matches:', pd.read_sql('SELECT COUNT(*) FROM matches', conn).iloc[0,0])
print('Players:', pd.read_sql('SELECT COUNT(*) FROM players', conn).iloc[0,0])
print('Rosters:', pd.read_sql('SELECT COUNT(*) FROM rosters', conn).iloc[0,0])
"
```

---

## Known Challenges

| Challenge | Mitigation |
|---|---|
| TTP table empty on load | `wait_for_selector` with 15s timeout; if still empty, check if login required |
| TTP rate limiting | Add 1–2s random delay between any retries |
| OE CSV missing T2 leagues | Some T2 leagues may be missing from OE entirely — use Leaguepedia as fallback |
| Leaguepedia tournament name changes | Query the Tournaments table at split start to discover correct names |
| Player name mismatches (TTP ↔ Leaguepedia) | Fuzzy match + manual review file |
| Roster changes mid-split | `snapshot_date` on roster records; always use the most recent one before match date |
