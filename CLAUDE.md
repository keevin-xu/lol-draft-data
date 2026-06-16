# LoL T2 Prediction Model — CLAUDE.md
*Two-person project. Read this before touching anything.*

---

## What This Project Does

Builds a win-probability model for League of Legends Tier 2 professional matches, then uses that model to find edge on Polymarket prediction markets.

The model has two rating layers:
1. **Solo queue rating** — raw individual player strength from ladder rank/LP (scraped from TrackingThePros)
2. **Pro match ELO** — team strength from official competitive results (scraped from Oracle's Elixir)

These blend dynamically: the less pro data a team has, the more the model leans on solo queue. See `docs/model_design.md` for the full math.

---

## Directory Structure

```
lol-prediction-model/
├── CLAUDE.md                  ← you are here
├── data/
│   ├── raw/                   ← never edit; scraped output goes here
│   │   ├── trackingthepros/   ← player soloq snapshots (daily)
│   │   ├── oracleselixir/     ← pro match CSVs by year
│   │   └── rosters/           ← current team roster snapshots (daily)
│   └── processed/             ← cleaned/joined data for model input
├── scrapers/
│   ├── ttp_scraper.py         ← TrackingThePros player + soloq data
│   ├── oe_scraper.py          ← Oracle's Elixir match CSV downloader
│   └── roster_scraper.py      ← current team rosters (day-of)
├── model/
│   ├── soloq_rating.py        ← convert rank/LP → numerical rating
│   ├── pro_elo.py             ← team ELO from match results
│   ├── blend.py               ← dynamic alpha blend of soloq + pro ELO
│   └── predict.py             ← final win probability output
├── backtest/
│   └── backtest.py            ← simulated P&L over historical data
├── polymarket/
│   ├── scanner.py             ← find open LoL T2 markets
│   └── edge.py                ← compare model prob vs market prob
├── db/
│   └── schema.sql             ← SQLite schema
├── docs/
│   ├── model_design.md        ← full math and design decisions
│   └── data_sources.md        ← how each source works, gotchas
├── tests/
│   └── ...
├── requirements.txt
└── .env.example               ← env vars needed (no secrets in git)
```

---

## Data Sources

### 1. TrackingThePros — Solo Queue Ratings
**URL:** https://www.trackingthepros.com/players  
**What we get:** Pro player → summoner account(s) → current rank + LP + server  
**Method:** Playwright headless browser (site is JS-rendered; no public API)  
**Scraper:** `scrapers/ttp_scraper.py`  
**Schedule:** Daily snapshot, saved to `data/raw/trackingthepros/YYYY-MM-DD.json`  

Key fields we extract:
- `player_name` — pro player handle
- `role` — Top/Jungle/Mid/ADC/Support
- `team` — current team
- `region` — NA/EU/KR/BR/etc.
- `accounts[]` — list of summoner names with server + rank + LP

**Gotcha:** One pro may have multiple accounts. We take the **highest-ranked account** as the primary signal, not an average.

### 2. Oracle's Elixir — Pro Match Results
**URL:** https://oracleselixir.com/tools/downloads  
**Direct S3:** `https://oracleselixir-downloadable-match-data.s3-us-west-2.amazonaws.com/{YEAR}_LoL_esports_match_data_from_OraclesElixir.csv`  
**Method:** Direct HTTP download — no browser needed, just `requests`  
**Scraper:** `scrapers/oe_scraper.py`  
**Schedule:** Download 2024 + 2025 + 2026 CSVs on first run; update 2026 weekly  

Key columns we use:
- `gameid`, `date`, `league`, `patch`, `side` (Blue/Red), `position`
- `teamname`, `playername`, `result` (1=win, 0=loss)
- `gamelength`, `kills`, `deaths`, `assists`, `totalgold`

**T2 league filter** — include only these `league` values:
```
NACL, LCK CL, LEC (ERLs), LLA, CBLOL, TCL, VCS, PCS, LJL, LCO
```
Exclude: LCS, LEC, LCK, LPL, MSI, Worlds (those are T1).

### 3. Current Rosters — Day-of Snapshot
**Source:** Leaguepedia API (MediaWiki) or TrackingThePros team pages  
**What we get:** Active 5-man roster for each team on match day  
**Scraper:** `scrapers/roster_scraper.py`  
**Schedule:** Run morning of any match day  

This is critical for the soloq blend — if a player subbed in yesterday, the roster from last week is wrong.

---

## Model Design (Summary)

Full details in `docs/model_design.md`. Quick reference:

```python
# 1. SoloQ rating per player (Master+ uses log compression)
soloq_rating = tier_base + 400 * log(1 + LP / 400)  # Master+
soloq_rating = tier_base + LP                         # below Master

# 2. Team soloq rating (weighted by role)
team_soloq = 0.20*top + 0.22*jungle + 0.23*mid + 0.20*bot + 0.15*support

# 3. Normalize to ELO scale
team_soloq_elo = 1500 + 100 * zscore(team_soloq)

# 4. Pro ELO update after each match
K = 32  # higher for new/T2 teams
expected = 1 / (1 + 10**((opp_elo - team_elo) / 400))
new_elo = old_elo + K * (result - expected)

# 5. Dynamic blend (more pro data → trust pro ELO more)
alpha = games_played / (games_played + 10)
final_rating = alpha * pro_elo + (1 - alpha) * team_soloq_elo

# 6. Win probability
P_win = 1 / (1 + 10**(-(rating_A - rating_B) / 400))
```

**Parameters to fit later via backtest:** role weights, K-factor, blend denominator (10), scale (400), blue-side offset.

---

## Today's Sprint

### Goal: Working scrapers for soloq + pro match data + day-of rosters

**Task 1 — OE scraper** (simpler, do first):
- `oe_scraper.py`: download 2024/2025/2026 CSVs, filter T2 leagues, save to `data/raw/oracleselixir/`
- Parse into SQLite `matches` + `games` tables

**Task 2 — TTP scraper** (harder, needs Playwright):
- `ttp_scraper.py`: launch headless browser, wait for table, extract all rows
- Save daily snapshot to `data/raw/trackingthepros/YYYY-MM-DD.json`
- Parse into SQLite `players` + `accounts` tables

**Task 3 — Roster scraper**:
- `roster_scraper.py`: pull current active rosters from Leaguepedia for all T2 leagues
- Map player handles to their TTP entries
- Save to `data/raw/rosters/YYYY-MM-DD.json`

See `docs/scraper_roadmap.md` for the detailed step-by-step plan.

---

## Dev Setup

```bash
# Clone and install
git clone <repo>
cd lol-prediction-model
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# Copy env file
cp .env.example .env
# Fill in any API keys

# Init database
python db/init_db.py

# Run scrapers
python scrapers/oe_scraper.py       # download OE match data
python scrapers/ttp_scraper.py      # scrape TTP player/soloq data
python scrapers/roster_scraper.py   # pull today's rosters
```

---

## Coding Conventions

- **Python 3.11+**
- All scrapers write raw output to `data/raw/` before any processing — never skip the raw save step
- Raw files are named `YYYY-MM-DD.json` or `YYYY-MM-DD.csv` for traceability
- All DB writes go through functions in `db/` — no raw SQL scattered in scrapers
- Use `loguru` for logging (`from loguru import logger`)
- Time-based train/val split only — never random (data leakage)
- Every function that calls a URL should have a retry with exponential backoff
- Don't commit data files — `data/` is in `.gitignore`

---

## Key Decisions Log

| Decision | Choice | Reason |
|---|---|---|
| SoloQ source | TrackingThePros | Best T2 coverage, includes EU/KR/NA |
| Pro match source | Oracle's Elixir | Free CSV, reliable, good T2 coverage |
| DB | SQLite → Postgres | SQLite is fine until we need concurrent writes |
| Headless browser | Playwright | More reliable than Selenium for modern React |
| ELO K-factor | 32 (tunable) | Higher than T1 default due to T2 instability |
| Blend denominator | 10 games | At 10 games, 50/50 pro vs soloq |

---

## Open Questions

- [ ] Which T2 regions are we prioritizing first? (affects roster scraper scope)
- [ ] Are we placing bets manually or via Polymarket API?
- [ ] What's the starting bankroll / max bet size?
- [ ] Do we want patch-level features in v1 or defer to v2?
