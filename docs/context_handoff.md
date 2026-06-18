# Project Context — Handoff Note
*Last updated: 2026-06-17*

---

## What We're Building

A win-probability model for League of Legends **Tier 2 professional matches** to find betting edge on Polymarket prediction markets. The model blends two rating signals:
1. **Solo queue rating** — raw individual player strength (scraped from TrackingThePros)
2. **Pro match ELO** — team strength from official match results (scraped from Oracle's Elixir)

---

## Current Status: Scrapers (3 of 3)

### ✅ Scraper 1 — Oracle's Elixir (`scrapers/oe_scraper.py`) — DONE
Downloads annual match CSVs from Google Drive, filters to T2 leagues, and upserts into SQLite.

**Result:** 10,372 T2 matches loaded into `db/lol_model.db` across 2024–2026:
- LCKC, EM, NACL, LJL, LFL, PCS, TCL, NLC, LVP SL, VCS, LRN, LRS, ESLOL, LTA S/N, LLA, LCO

**Note:** OE no longer uses the S3 URL documented in the roadmap. Files now live in a public Google Drive folder. The scraper handles this automatically.

---

### ✅ Scraper 2 — TrackingThePros (`scrapers/ttp_scraper.py`) — DONE
Paginates TTP's internal DataTables API to pull all pro player soloq data.

**Result:** 2,291 players in SQLite with rank/LP/rating:
- Challenger: 227, Grandmaster: 203, Master: 372, Diamond: 312
- Raw daily snapshot at `data/raw/trackingthepros/2026-06-16.json`

**Key gotcha:** TTP's rank strings have inconsistent formats ("Challenger 3,744LP", "Diamond II", "Ch 1,200LP"). The rank parser handles all variants with a carefully ordered regex — **do not touch the regex in `parse_rank()` without testing the full rank distribution.**

---

### 🔄 Scraper 3 — Roster Scraper (`scrapers/roster_scraper.py`) — IN PROGRESS (~90%)

Pulls current team rosters from the **Leaguepedia Cargo API**, fuzzy-matches player names against TTP data, and upserts to the `rosters` SQLite table.

**Code is written.** Currently blocked on Leaguepedia API rate limiting — we hit it hard during development/testing and need to wait for the limit to clear before doing a clean end-to-end run.

**Run it with:**
```bash
python scrapers/roster_scraper.py
```

---

## Active Issues

### 1. Leaguepedia Rate Limiting
We were querying the Leaguepedia Cargo API heavily while figuring out the correct table structure. The API (`lol.fandom.com/api.php`) is now returning `ratelimited` errors. The scraper has exponential backoff built in (up to 6 retries, 30s×N waits), but the limit needs time to fully clear.

**Fix:** Wait ~30 minutes, then run the roster scraper. It will self-heal via backoff if partially rate-limited.

### 2. Leaguepedia Field Name Was Wrong
The Tournaments table has **no `Tier` field**. The roadmap documentation is incorrect. Correct field is `TournamentLevel = 'Secondary'` for T2 leagues.

### 3. League Name Mapping Gaps (Roster Scraper)
Leaguepedia uses full league names; OE uses abbreviations. We've confirmed 12 of ~18 mappings. These OE abbreviations still need their Leaguepedia full names verified:

| OE Abbreviation | Status | Notes |
|---|---|---|
| `ESLOL` | ❓ Unknown | Italian league — not found in 2026 Secondary list yet |
| `LCO` | ❓ Unknown | Oceania — may be rebranded/defunct |
| `LEC` | ❓ Unclear | OE uses "LEC" for what appears to be EU T2; Leaguepedia "LEC" = T1 championship |
| `LTA N` | ❓ Unknown | Latin America North (post-2025 LLA rebrand) |
| `LTA S` | ❓ Unknown | Latin America South |
| `CBLOL Academy` | ❓ Unknown | Brazil academy league |

The roster scraper will skip these leagues until the mapping is filled in. Edit `LEAGUE_MAP` in `scrapers/roster_scraper.py` to add them once identified.

---

## Confirmed Leaguepedia → OE Mappings

```python
LEAGUE_MAP = {
    "North American Challengers League": "NACL",
    "LCK Challengers League":            "LCKC",
    "EMEA Masters":                      "EM",
    "Northern League of Legends Championship": "NLC",
    "La Ligue Française":                "LFL",
    "LVP SuperLiga":                     "LVP SL",
    "Turkish Championship League":       "TCL",
    "Pacific Championship Series":       "PCS",
    "Vietnam Championship Series":       "VCS",
    "LoL Japan League":                  "LJL",
    "Liga Regional Norte":               "LRN",
    "Liga Regional Sur":                 "LRS",
}
```

---

## Data Files

| File | Contents |
|---|---|
| `db/lol_model.db` | SQLite: matches (10,372), players (2,291), accounts (2,291) |
| `data/raw/oracleselixir/2024.csv` | OE match data 2024 |
| `data/raw/oracleselixir/2025.csv` | OE match data 2025 |
| `data/raw/oracleselixir/2026.csv` | OE match data 2026 (weekly refresh) |
| `data/raw/trackingthepros/2026-06-16.json` | TTP snapshot (2,291 players) |
| `data/raw/rosters/` | Daily roster snapshots (empty until scraper 3 runs) |
| `data/processed/unmatched_players.json` | Players from Leaguepedia with no TTP match |

---

## What's Next After Scrapers

1. **`model/soloq_rating.py`** — convert player rank/LP to numeric rating (formula already in CLAUDE.md)
2. **`model/pro_elo.py`** — compute team ELO from historical match results
3. **`model/blend.py`** — dynamic alpha blend of soloq + pro ELO
4. **`model/predict.py`** — output win probability for a given matchup
5. **`backtest/backtest.py`** — validate model on historical data
6. **`polymarket/scanner.py`** — find open LoL T2 markets on Polymarket
7. **`polymarket/edge.py`** — compare model probability vs market implied probability

---

## Quick Start

```bash
cd lol-prediction-model
source venv/bin/activate

# Scrapers (run daily)
python scrapers/oe_scraper.py        # ~weekly for 2026 data
python scrapers/ttp_scraper.py       # daily soloq snapshot
python scrapers/roster_scraper.py    # day-of roster check

# Inspect DB
sqlite3 db/lol_model.db ".tables"
sqlite3 db/lol_model.db "SELECT league, COUNT(*) FROM matches GROUP BY league"
```
