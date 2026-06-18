# LoL T2 Prediction Model — Full System Deep Dive

#lol #model #prediction #polymarket

---

## What This System Does

This system predicts **win probabilities for Tier 2 professional League of Legends matches**, then compares those probabilities against Polymarket betting odds to find +EV (positive expected value) bets.

The pipeline has two layers:

```
┌─────────────────────────────────────────────┐
│             DATA LAYER (Scrapers)           │
│                                             │
│  oe_scraper.py ──► match history (10,372)   │
│  ttp_scraper.py ──► player soloq (2,291)    │
│  roster_scraper.py ──► team rosters (598)   │
└──────────────────┬──────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────┐
│            MODEL LAYER (Ratings)            │
│                                             │
│  soloq_rating.py ──► player strength        │
│  pro_elo.py ──► team ELO from match history │
│  blend.py ──► merge soloq + pro ELO         │
│  predict.py ──► P(Team A wins)              │
└─────────────────────────────────────────────┘
```

---

## The Core Idea

> [!info] Two Signals, One Rating
> **Signal 1 — Solo Queue:** How good are the individual players on ladder? A team of 5 Challengers is probably better than a team of 5 Masters.
> **Signal 2 — Pro ELO:** How does the team perform in actual competitive matches? A team that keeps winning should be rated higher.
> 
> **The blend:** New teams with few pro matches lean on soloq. Experienced teams lean on pro ELO. The model interpolates dynamically.

---

## Data Layer — What We Scrape

### 1. Oracle's Elixir — Pro Match Results

**File:** `scrapers/oe_scraper.py`
**Source:** Public Google Drive folder maintained by Oracle's Elixir
**What it gives us:** Every T2 professional match result (2024–2026)

| Stat | Value |
|---|---|
| Total T2 matches | **10,372** |
| Years covered | 2024, 2025, 2026 |
| Leagues covered | 18 T2 leagues worldwide |

**Match breakdown by league:**

| League | Region | Matches |
|---|---|---|
| LCKC | Korea | 1,391 |
| EM | EMEA Masters | 1,122 |
| NACL | North America | 902 |
| LJL | Japan | 848 |
| LEC | Europe (old ERL umbrella) | 846 |
| LFL | France | 734 |
| PCS | Taiwan/HK/Macao | 647 |
| TCL | Turkey | 564 |
| NLC | Nordic | 559 |
| LVP SL | Spain | 506 |
| VCS | Vietnam | 499 |
| LRN | Brazil North | 331 |
| LRS | Brazil South | 329 |
| ESLOL | Italy | 291 |
| LTA S | Latin America South | 222 |
| LTA N | Latin America North | 214 |
| LLA | Latin America (pre-rebrand) | 213 |
| LCO | Oceania | 154 |

Each match row in SQLite contains:
```
gameid | date | league | patch | playoffs | blue_team | red_team | winner | gamelength
```

**How the scraper works:**
1. Checks the datalisk.io API for latest update timestamps
2. Downloads CSVs from Google Drive (one per year)
3. Filters to `T2_LEAGUES` set (18 league abbreviations)
4. Collapses 2 team rows per game into 1 match row (blue vs red → winner)
5. `INSERT OR IGNORE` into SQLite `matches` table

> [!warning] Data Source Changed
> The old S3 bucket URL (`oracleselixir-downloadable-match-data.s3-us-west-2.amazonaws.com`) is dead. The scraper now uses Google Drive direct downloads. File IDs are hardcoded as fallbacks and can be auto-discovered via Playwright if they change.

---

### 2. TrackingThePros — Solo Queue Ratings

**File:** `scrapers/ttp_scraper.py`
**Source:** TrackingThePros internal DataTables API
**What it gives us:** Every T2 pro player's solo queue rank and LP

| Stat | Value |
|---|---|
| Total players | **2,291** |
| Players with rank data | **1,161** (50.6%) |
| Unranked/inactive | **1,139** (49.4%) |

**Rank distribution:**

| Tier | Count | Typical Rating Range |
|---|---|---|
| Challenger | 225 | 3,400–3,733 |
| Grandmaster | 206 | 3,100–3,400 |
| Master | 372 | 2,800–3,100 |
| Diamond | 312 | 2,400–2,700 |
| Platinum | 39 | 1,600–1,900 |
| Gold/Silver/Bronze | 7 | 400–1,200 |
| Unranked | 1,139 | 0 (excluded from model) |

**How the scraper works:**
1. Paginates TTP's `/d/list_players` DataTables API (200 per page)
2. Parses rank strings like `"Challenger 3,744LP"`, `"Diamond II"`, `"Ch 1,200LP"`
3. Converts to numeric rating using log compression for Master+
4. Saves daily JSON snapshot to `data/raw/trackingthepros/YYYY-MM-DD.json`
5. Upserts to `players` + `accounts` tables

> [!important] Rank Parser Regex
> The regex in `parse_rank()` is carefully ordered to handle edge cases. The division pattern uses explicit alternatives `(IV|III|II|I)` in that order — longest first. **Do not simplify this regex** without testing the full rank distribution. An earlier version with `I{0,3}V?` silently matched empty strings and broke Challenger LP parsing.

---

### 3. Leaguepedia — Current Rosters

**File:** `scrapers/roster_scraper.py`
**Source:** Leaguepedia Cargo API (`lol.fandom.com/api.php`)
**What it gives us:** Which 5 players are on each team right now

| Stat | Value |
|---|---|
| Roster entries | **598** |
| Teams with rosters | **97** |
| Teams in matches with NO roster | **343** |

**How the scraper works:**
1. Queries `Tournaments` table for `TournamentLevel='Secondary'` AND `Year='2026'`
2. Filters to leagues in `LEAGUE_MAP` (Leaguepedia full name → OE abbreviation)
3. For each tournament, queries `TournamentRosters` table
4. Parses `;;`-delimited `RosterLinks` and `Roles` fields
5. Strips coaching staff (Coach, Manager, Analyst)
6. Fuzzy-matches player names against TTP data (cutoff=0.85)
7. Saves to `data/raw/rosters/YYYY-MM-DD.json` and `rosters` table

> [!warning] Leaguepedia Rate Limiting
> The API rate-limits aggressively. The scraper has exponential backoff (30s × attempt), but if you've been testing queries manually, you may need to wait ~30 minutes before running. The scraper uses `POST` requests and a `User-Agent` with contact email.

> [!note] Incomplete League Mapping
> 6 of 18 OE leagues don't have confirmed Leaguepedia names yet:
> `LCO`, `ESLOL`, `LEC`, `LTA N`, `LTA S`, `CBLOL Academy`
> 
> These leagues are skipped. Edit `LEAGUE_MAP` in `roster_scraper.py` to add them.

---

## Model Layer — How Ratings Work

### Step 1: `soloq_rating.py` — Player Strength Score

**What it does:** Converts a player's ladder rank + LP into a single numeric rating.

**The formula:**

```python
# Tier base values (each tier = 400 points)
TIER_BASE = {
    "Iron": 0, "Bronze": 400, "Silver": 800, "Gold": 1200,
    "Platinum": 1600, "Emerald": 2000, "Diamond": 2400,
    "Master": 2800, "Grandmaster": 2800, "Challenger": 2800,
}

# Below Master: linear
# Diamond II 72LP → 2400 + 200 (div II) + 72 = 2672

# Master+: log compressed
# Challenger 3744LP → 2800 + 400 × ln(1 + 3744/400)
#                    → 2800 + 400 × ln(10.36)
#                    → 2800 + 400 × 2.338
#                    → 2800 + 935.2
#                    → 3735.2
```

> [!info] Why log compression?
> Without it, a 2000 LP Challenger would be rated as 50× better than a fresh Master player. In reality, the skill gap between 0 LP Master and 2000 LP Challenger is significant but not proportional to the LP difference. `log(1 + LP/400)` smooths this curve so diminishing returns kick in at high LP.

**Worked example — computing a team's soloq rating:**

```
Galions roster:
  Top     Carlsen    rating = 3408
  Jungle  Thayger    rating = 3535
  Mid     OMON       rating = 3585
  Bot     HARPOON    rating = 0 (unranked → excluded)
  Support Zoelys     rating = 3616

Role weights (redistributed since Bot is missing):
  Top:     0.20 / 0.80 = 0.250
  Jungle:  0.22 / 0.80 = 0.275
  Mid:     0.23 / 0.80 = 0.2875
  Support: 0.15 / 0.80 = 0.1875

team_soloq = 0.250×3408 + 0.275×3535 + 0.2875×3585 + 0.1875×3616
           = 852 + 972.1 + 1030.7 + 678.0
           = 3532.8
```

**Key functions:**

| Function | Purpose |
|---|---|
| `compute_all_ratings()` | Batch recompute all `accounts.soloq_rating` from rank_tier + lp |
| `get_player_rating(name)` | Best (MAX) soloq_rating for a player across all accounts |
| `get_team_player_ratings(team)` | Returns `{role: rating}` for a team's roster — picks highest-rated player per role |

---

### Step 2: `pro_elo.py` — Team ELO from Match Results

**What it does:** Processes all 10,372 matches chronologically and computes an ELO rating for every team that has played.

**The ELO system:**

```python
K = 32  # How much each match shifts ELO (higher = more volatile)

# Before a match: compute expected win probability
expected = 1 / (1 + 10^((opponent_elo - team_elo) / 400))

# After a match: update ELO
new_elo = old_elo + K × (actual_result - expected)
# actual_result = 1.0 if won, 0.0 if lost
```

**Worked example:**

```
Match: Galions (elo=1800) vs Solary (elo=1750)
Galions expected = 1 / (1 + 10^((1750-1800)/400))
                 = 1 / (1 + 10^(-0.125))
                 = 1 / (1 + 0.749)
                 = 0.572 (57.2% expected win rate)

Result: Galions wins (actual = 1.0)

Galions new_elo = 1800 + 32 × (1.0 - 0.572) = 1800 + 13.7 = 1813.7
Solary new_elo  = 1750 + 32 × (0.0 - 0.428) = 1750 - 13.7 = 1736.3
```

> [!info] ELO is Zero-Sum
> Every point one team gains, the other loses. The average ELO across all teams stays at ~1500 forever. This is by design — it measures *relative* strength, not absolute skill.

**Soloq baseline initialization:**

Teams with roster data get a "warm start" instead of the default 1500:

1. Compute raw `team_soloq` (weighted player ratings, see Step 1)
2. Z-score normalize across all teams with data: `soloq_elo = 1500 + 100 × zscore`
3. Initialize ELO from `soloq_elo` instead of 1500

This only matters for the first ~10 games. After that, match results dominate.

**Current stats:**

| Metric | Value |
|---|---|
| Teams rated | **440** |
| Highest ELO | Galions — **1919.6** (171 games) |
| Lowest ELO | V3 Esports — **1218.4** (107 games) |
| Average ELO | **1500.0** (by definition) |
| Teams with soloq baseline | **29** of 440 |

**Top 5 teams:**

| Team | ELO | Games | League |
|---|---|---|---|
| Galions | 1919.6 | 171 | LFL |
| Solary | 1894.5 | 170 | LFL |
| PSG Talon | 1892.8 | 71 | PCS |
| G2 Esports | 1802.6 | 219 | EM |
| REJECT | 1798.8 | 54 | LJL |

**Bottom 5 teams:**

| Team | ELO | Games | League |
|---|---|---|---|
| V3 Esports | 1218.4 | 107 | LJL |
| HELL PIGS | 1239.1 | 96 | PCS |
| Rainbow Warriors | 1255.7 | 24 | LCO |
| Six Karma | 1265.0 | 50 | LLA |
| Radical Pop Gaming | 1286.7 | 22 | LRS |

**Key functions:**

| Function | Purpose |
|---|---|
| `compute_team_soloq(team)` | Weighted soloq sum for a team (None if <3 roles have data) |
| `normalize_to_elo(scores)` | Z-score normalize raw soloq → 1500-centered ELO scale |
| `run_elo(soloq_elos)` | **Core pipeline** — processes all matches, returns `{team: {elo, games}}` |
| `save_to_db(results)` | Upserts into `teams` table |
| `get_team_soloq_elos()` | Public API for blend.py (cached) |

---

### Step 3: `blend.py` — Dynamic Alpha Blending

**What it does:** Merges pro ELO and soloq baseline, weighting by experience.

**The formula:**

```python
alpha = games_played / (games_played + blend_k)  # blend_k=10 by default

final_rating = alpha × pro_elo + (1 - alpha) × soloq_elo
```

**How alpha changes with experience:**

| Games Played | Alpha | Meaning |
|---|---|---|
| 0 | 0.00 | 100% soloq, 0% pro |
| 5 | 0.33 | 33% pro, 67% soloq |
| 10 | 0.50 | 50/50 |
| 20 | 0.67 | 67% pro, 33% soloq |
| 30 | 0.75 | 75% pro |
| 50 | 0.83 | 83% pro |
| 100 | 0.91 | 91% pro |
| 170 | 0.94 | 94% pro |

**Worked example:**

```
FENNEL:  pro_elo=1751.6  soloq_elo=1333  games=38
  alpha = 38 / (38 + 10) = 0.79
  blended = 0.79 × 1751.6 + 0.21 × 1333 = 1383.8 + 279.9 = 1663.7

FlyQuest: pro_elo=1755.0  soloq_elo=1500 (no roster data → default)  games=54
  alpha = 54 / (54 + 10) = 0.84
  blended = 0.84 × 1755.0 + 0.16 × 1500 = 1474.2 + 240.0 = 1714.2
```

> [!tip] `blend_k` is Tunable
> The default `blend_k=10` means 50/50 at 10 games. The backtest will optimize this. A higher `blend_k` (e.g. 20) would trust soloq longer; lower (e.g. 5) would trust pro results faster. This is why blend.py does NOT write to DB — the parameter changes during optimization.

**Key functions:**

| Function | Purpose |
|---|---|
| `compute_blended_rating(pro, soloq, games, k)` | Pure math — no DB access |
| `get_team_rating(team_name)` | Reads DB + soloq, returns blended rating |
| `get_all_ratings()` | Batch version for all teams |

---

### Step 4: `predict.py` — Win Probability

**What it does:** Takes two teams, gets their blended ratings, outputs P(A wins).

**The formula:**

```python
P(A wins) = 1 / (1 + 10^(-(rating_A - rating_B) / 400))
```

**Worked example:**

```
Solary (rating=1872.1) vs Karmine Corp (rating=1724.2)

diff = 1872.1 - 1724.2 = 147.9
P(Solary wins) = 1 / (1 + 10^(-147.9/400))
               = 1 / (1 + 10^(-0.370))
               = 1 / (1 + 0.427)
               = 0.701 → 70.1%
```

**What rating differences mean:**

| Rating Diff | Win% for Favorite |
|---|---|
| 0 | 50.0% |
| 50 | 57.2% |
| 100 | 64.0% |
| 150 | 70.1% |
| 200 | 75.9% |
| 300 | 84.9% |
| 400 | 90.9% |

**Key functions:**

| Function | Purpose |
|---|---|
| `win_probability(a, b, scale)` | Pure math — P(A wins) |
| `predict_match(team_a, team_b)` | End-to-end: DB → blend → probability |
| `predict_from_ratings(a, b)` | For backtest.py (takes pre-computed ratings) |

---

## Complete Data Flow

```
                    ┌──────────────────┐
                    │  Oracle's Elixir │ ◄── Google Drive CSVs
                    │   (oe_scraper)   │     2024/2025/2026
                    └────────┬─────────┘
                             │
                             ▼
                    ┌──────────────────┐
                    │  matches table   │  10,372 rows
                    │  (gameid, date,  │  blue_team, red_team,
                    │   league, winner)│  winner="blue"/"red"
                    └────────┬─────────┘
                             │
    ┌──────────────┐         │         ┌──────────────────┐
    │TrackingThePros│        │         │   Leaguepedia    │
    │ (ttp_scraper) │        │         │ (roster_scraper) │
    └──────┬───────┘         │         └────────┬─────────┘
           │                 │                  │
           ▼                 │                  ▼
    ┌──────────────┐         │         ┌──────────────────┐
    │players table │         │         │  rosters table   │
    │accounts table│         │         │  (team, player,  │
    │(rank, LP,    │         │         │   role, date)    │
    │ soloq_rating)│         │         │  598 rows        │
    └──────┬───────┘         │         └────────┬─────────┘
           │                 │                  │
           ▼                 │                  │
    ╔══════════════╗         │                  │
    ║soloq_rating  ║ ◄───────┼──────────────────┘
    ║   .py        ║         │
    ║              ║         │
    ║ rank→rating  ║         │
    ║ team aggr.   ║         │
    ╚══════╤═══════╝         │
           │                 │
           ▼                 ▼
    ╔══════════════════════════════╗
    ║        pro_elo.py            ║
    ║                              ║
    ║  1. team soloq baseline      ║
    ║  2. normalize to ELO scale   ║
    ║  3. process 10,372 matches   ║
    ║  4. update ELO per match     ║
    ╚══════════════╤═══════════════╝
                   │
                   ▼
          ┌──────────────────┐
          │   teams table    │  440 rows
          │  (pro_elo,       │  ELO range: 1218–1920
          │   games_played)  │
          └────────┬─────────┘
                   │
                   ▼
          ╔══════════════════╗
          ║    blend.py      ║
          ║                  ║
          ║  α × pro_elo +   ║
          ║  (1-α) × soloq   ║
          ╚════════╤═════════╝
                   │
                   ▼
          ╔══════════════════╗
          ║   predict.py     ║
          ║                  ║
          ║  P(A wins) =     ║
          ║  1/(1+10^(-Δ/400)║
          ╚════════╤═════════╝
                   │
                   ▼
          ┌──────────────────┐
          │  Win Probability │
          │  e.g. 70.1%      │
          └──────────────────┘
                   │
         ┌─────────┴─────────┐
         ▼                   ▼
  ┌──────────────┐   ┌──────────────┐
  │ backtest.py  │   │ edge.py      │
  │ (validate    │   │ (compare vs  │
  │  on history) │   │  Polymarket) │
  └──────────────┘   └──────────────┘
```

---

## Database Schema

```sql
-- Player identity
players (id, player_name UNIQUE, role, team, region, updated_at)

-- Soloq accounts (1 player can have multiple)
accounts (id, player_id→players, summoner_name, server,
          rank_tier, lp, soloq_rating, snapshot_date)

-- Team ratings (populated by pro_elo.py)
teams (id, team_name UNIQUE, region, league,
       pro_elo DEFAULT 1500.0, games_played DEFAULT 0, updated_at)

-- Match results (from OE scraper)
matches (id, gameid UNIQUE, date, league, patch, playoffs,
         blue_team, red_team, winner, gamelength)

-- Current rosters (from Leaguepedia)
rosters (id, team, player_name, role, snapshot_date, tournament)
```

**Current row counts:**

| Table | Rows | Populated By |
|---|---|---|
| players | 2,291 | ttp_scraper.py |
| accounts | 2,300 | ttp_scraper.py |
| matches | 10,372 | oe_scraper.py |
| rosters | 598 | roster_scraper.py |
| teams | 440 | pro_elo.py |

---

## How to Run Everything

### Initial Setup

```bash
cd lol-prediction-model
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
python db/init_db.py
```

### Daily Scraper Pipeline

```bash
# 1. Update match data (weekly for 2026, cached for older years)
python scrapers/oe_scraper.py

# 2. Update soloq ratings (daily snapshot)
python scrapers/ttp_scraper.py

# 3. Update rosters (run on match days)
python scrapers/roster_scraper.py
```

### Model Pipeline

```bash
# 1. Recompute player soloq ratings (optional — TTP scraper does this on insert)
python model/soloq_rating.py

# 2. Run full ELO engine (processes all matches, populates teams table)
python model/pro_elo.py

# 3. View blended leaderboard
python model/blend.py

# 4. Predict a specific matchup
python model/predict.py "Solary" "Karmine Corp"

# 5. List all teams by rating
python model/predict.py --list
```

### Quick DB Queries

```bash
# Top teams by ELO
sqlite3 db/lol_model.db "SELECT team_name, pro_elo, games_played FROM teams ORDER BY pro_elo DESC LIMIT 20;"

# Matches for a specific team
sqlite3 db/lol_model.db "SELECT date, blue_team, red_team, winner FROM matches WHERE blue_team='Solary' OR red_team='Solary' ORDER BY date DESC LIMIT 10;"

# Player soloq ratings
sqlite3 db/lol_model.db "SELECT p.player_name, a.rank_tier, a.lp, a.soloq_rating FROM players p JOIN accounts a ON a.player_id = p.id WHERE a.soloq_rating > 0 ORDER BY a.soloq_rating DESC LIMIT 20;"

# Team roster with ratings
sqlite3 db/lol_model.db "SELECT r.team, r.player_name, r.role, a.soloq_rating FROM rosters r LEFT JOIN players p ON LOWER(r.player_name) = LOWER(p.player_name) LEFT JOIN accounts a ON a.player_id = p.id WHERE r.team='Galions';"
```

---

## Graceful Degradation

The model is designed to work with incomplete data:

| Condition | What Happens |
|---|---|
| Team has no roster data (343 of 440 teams) | Starts at default 1500 ELO, calibrates purely through match results |
| Player in roster but unranked/no soloq data | Treated as missing role — weight redistributed among present roles |
| Team has roster but <3 roles have ratings | Falls back to 1500 default (soloq baseline too unreliable) |
| Team never appears in match history | Not in teams table — `predict.py` uses 1500 with a warning |
| Roster scraper blocked by rate limit | Model still works from match data alone — soloq is enrichment |

---

## Tunable Parameters

These are the knobs the backtest will optimize:

| Parameter | Default | Location | Effect |
|---|---|---|---|
| `K` (ELO K-factor) | 32 | `pro_elo.py` | Higher = more volatile ratings, faster to react |
| `blend_k` | 10 | `blend.py` | Higher = trust soloq longer before switching to pro |
| `scale` | 400 | `predict.py` | Higher = rating differences matter less |
| `ROLE_WEIGHTS` | Top:0.20 Jng:0.22 Mid:0.23 Bot:0.20 Sup:0.15 | `pro_elo.py` | How much each position contributes to team soloq |
| `MIN_ROLES_FOR_BASELINE` | 3 | `pro_elo.py` | Min roles needed to compute soloq baseline |
| `FUZZY_CUTOFF` | 0.85 | `roster_scraper.py` | Player name matching strictness |

---

## Known Limitations

1. **Roster data is a single snapshot** — no historical rosters. The soloq baseline reflects current teams, not who played in 2024 matches. This means the soloq initialization is biased toward current compositions. Not a major issue because ELO self-corrects after ~10 games.

2. **Division data lost for below-Master tiers** — the accounts table stores `rank_tier` but not `division`. A Diamond II player and Diamond IV player get the same base rating in batch recomputation. The TTP scraper stores the correct rating on insert, so this only matters if you manually recalculate.

3. **6 leagues missing from roster mapping** — LCO, ESLOL, LEC, LTA N, LTA S, CBLOL Academy don't have confirmed Leaguepedia names. These teams still get ELO from match history, just no soloq baseline.

4. **Cross-region ELO** — teams from different leagues never play each other (except at EMEA Masters). A 1700 ELO LFL team and a 1700 ELO LJL team aren't necessarily equal strength. The EMEA Masters (EM) cross-regional tournament helps calibrate European ERLs against each other, but Asia/Americas leagues are isolated.

5. **No patch-level features** — game patches can shift the meta significantly. The current model treats all patches equally. Adding patch features is planned for v2.

---

## What's Next

| Component | Status | Purpose |
|---|---|---|
| `backtest/backtest.py` | 🔲 Not started | Simulate betting on historical matches, compute ROI, optimize parameters |
| `polymarket/scanner.py` | 🔲 Not started | Find open LoL T2 markets on Polymarket |
| `polymarket/edge.py` | 🔲 Not started | Compare model probability vs market implied probability, flag +EV bets |

---

## File Reference

```
model/
├── __init__.py          ← empty package file
├── soloq_rating.py      ← player strength from rank/LP
├── pro_elo.py           ← team ELO engine (most complex)
├── blend.py             ← dynamic alpha blending
└── predict.py           ← win probability output + CLI

scrapers/
├── __init__.py
├── oe_scraper.py        ← Oracle's Elixir match data
├── ttp_scraper.py       ← TrackingThePros soloq data
└── roster_scraper.py    ← Leaguepedia team rosters

db/
├── schema.sql           ← table definitions
├── init_db.py           ← creates/resets the database
└── lol_model.db         ← SQLite database (not in git)

data/
├── raw/
│   ├── oracleselixir/   ← 2024.csv, 2025.csv, 2026.csv
│   ├── trackingthepros/ ← YYYY-MM-DD.json snapshots
│   └── rosters/         ← YYYY-MM-DD.json snapshots
└── processed/
    └── unmatched_players.json
```
