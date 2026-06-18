"""
Team Pro ELO rating engine.

1. Computes a soloq-based baseline ELO for teams with roster data.
2. Processes all historical T2 matches chronologically to produce
   an ELO rating for every team that has played.
3. Writes results to the teams SQLite table.

Run standalone:  python model/pro_elo.py
"""

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

from functools import lru_cache

import numpy as np
from loguru import logger

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from model.soloq_rating import get_regional_soloq_averages, get_team_player_ratings

DB_PATH = _ROOT / "db" / "lol_model.db"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
K = 64
DEFAULT_ELO = 1500.0

ROLE_WEIGHTS = {
    "Top": 0.20,
    "Jungle": 0.22,
    "Mid": 0.23,
    "Bot": 0.20,
    "Support": 0.15,
}

HALF_LIFE_DAYS = 270  # ELO decays halfway to 1500 after this many days of inactivity

# Minimum number of roles with soloq data to compute a team baseline
MIN_ROLES_FOR_BASELINE = 3

# Maps OE league abbreviation → region code (matching players.region values)
LEAGUE_TO_REGION: Dict[str, str] = {
    "NACL": "NA",
    "LCKC": "KR",
    "EM": "EU",
    "LEC": "EU",
    "NLC": "EU",
    "LFL": "EU",
    "ESLOL": "EU",
    "LVP SL": "EU",
    "TCL": "TR",
    "LCO": "OCE",
    "LLA": "LAS",
    "LTA N": "LAS",
    "LTA S": "LAS",
    "LRN": "BR",
    "LRS": "BR",
    "CBLOL Academy": "BR",
    "PCS": "PCS",
    "VCS": "VN",
    "LJL": "JP",
}


# ---------------------------------------------------------------------------
# SoloQ-based team baseline
# ---------------------------------------------------------------------------
def compute_team_soloq(team: str) -> Optional[float]:
    """
    Weighted sum of roster player soloq ratings for a team.

    Returns None if fewer than MIN_ROLES_FOR_BASELINE roles have data.
    When some roles are missing, redistributes their weight proportionally.
    """
    ratings = get_team_player_ratings(team)
    present = {role: r for role, r in ratings.items() if r is not None and r > 0}

    if len(present) < MIN_ROLES_FOR_BASELINE:
        return None

    total_weight = sum(ROLE_WEIGHTS[role] for role in present)
    weighted_sum = sum(
        (ROLE_WEIGHTS[role] / total_weight) * rating
        for role, rating in present.items()
    )
    return weighted_sum


def compute_all_team_soloqs() -> Dict[str, float]:
    """
    Compute raw soloq scores for every team that has roster data.
    Returns {team_name: raw_soloq_score}.
    """
    conn = sqlite3.connect(DB_PATH)
    teams = [
        row[0]
        for row in conn.execute("SELECT DISTINCT team FROM rosters").fetchall()
    ]
    conn.close()

    scores: Dict[str, float] = {}
    for team in teams:
        score = compute_team_soloq(team)
        if score is not None:
            scores[team] = score

    logger.info(
        f"Soloq baselines: {len(scores)}/{len(teams)} roster teams have sufficient data"
    )
    return scores


def normalize_to_elo(soloq_scores: Dict[str, float]) -> Dict[str, float]:
    """
    Z-score normalize raw soloq scores onto a 1500-centered ELO scale.
    """
    if not soloq_scores:
        return {}

    values = np.array(list(soloq_scores.values()))
    mean = values.mean()
    std = values.std()

    if std == 0:
        return {team: DEFAULT_ELO for team in soloq_scores}

    return {
        team: DEFAULT_ELO + 100.0 * (score - mean) / std
        for team, score in soloq_scores.items()
    }


_cached_soloq_elos: Optional[Dict[str, float]] = None


def get_team_soloq_elos() -> Dict[str, float]:
    """Public API for blend.py — combines compute + normalize, cached."""
    global _cached_soloq_elos
    if _cached_soloq_elos is None:
        raw = compute_all_team_soloqs()
        _cached_soloq_elos = normalize_to_elo(raw)
    return _cached_soloq_elos


# ---------------------------------------------------------------------------
# Regional strength offsets
# ---------------------------------------------------------------------------
_cached_regional_offsets: Optional[Dict[str, float]] = None


def compute_regional_offsets() -> Dict[str, float]:
    """
    Compute ELO offsets per region based on soloq strength relative to global avg.
    Returns {"KR": +30, "EU": +28, "OCE": -80, ...}.
    """
    global _cached_regional_offsets
    if _cached_regional_offsets is not None:
        return _cached_regional_offsets

    avgs = get_regional_soloq_averages()
    if not avgs:
        _cached_regional_offsets = {}
        return {}

    values = np.array(list(avgs.values()))
    global_mean = values.mean()
    global_std = values.std()

    if global_std == 0:
        _cached_regional_offsets = {}
        return {}

    offsets = {
        region: 100.0 * (avg - global_mean) / global_std
        for region, avg in avgs.items()
    }
    _cached_regional_offsets = offsets

    logger.info("Regional ELO offsets (soloq-derived):")
    for region, offset in sorted(offsets.items(), key=lambda x: -x[1]):
        logger.info(f"  {region:5} {offset:+6.1f}  (soloq avg: {avgs[region]:.0f})")

    return offsets


def get_league_offset(league: str) -> float:
    """Map a league abbreviation to its regional ELO offset."""
    region = LEAGUE_TO_REGION.get(league)
    if not region:
        return 0.0
    offsets = compute_regional_offsets()
    return offsets.get(region, 0.0)


# ---------------------------------------------------------------------------
# ELO engine
# ---------------------------------------------------------------------------
def expected_score(elo_a: float, elo_b: float) -> float:
    return 1.0 / (1.0 + 10.0 ** ((elo_b - elo_a) / 400.0))


def _parse_date(date_str: str) -> datetime:
    """Parse date string from matches table (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)."""
    try:
        return datetime.fromisoformat(date_str)
    except ValueError:
        return datetime.strptime(date_str[:10], "%Y-%m-%d")


def _apply_decay(
    elo: float,
    days_since: int,
    half_life: float,
) -> float:
    """Decay ELO toward DEFAULT_ELO based on days of inactivity."""
    if days_since <= 0 or half_life >= 9000:
        return elo
    decay = 0.5 ** (days_since / half_life)
    return DEFAULT_ELO + (elo - DEFAULT_ELO) * decay


def run_elo(
    soloq_elos: Optional[Dict[str, float]] = None,
    half_life_days: float = HALF_LIFE_DAYS,
) -> Dict[str, Dict]:
    """
    Process all matches chronologically and compute ELO for every team.

    Before each match, decays both teams' ELOs toward DEFAULT_ELO based
    on days since their last match (half_life_days controls decay rate).
    Returns {team_name: {"elo": float, "games_played": int}}.
    """
    baselines = soloq_elos or {}
    regional_offsets = compute_regional_offsets()
    elos: Dict[str, float] = {}
    games: Dict[str, int] = {}
    team_league: Dict[str, str] = {}
    last_played: Dict[str, datetime] = {}

    def _get_elo(team: str, league: str) -> float:
        if team not in elos:
            if team in baselines:
                elos[team] = baselines[team]
            else:
                region = LEAGUE_TO_REGION.get(league, "")
                offset = regional_offsets.get(region, 0.0)
                elos[team] = DEFAULT_ELO + offset
            team_league[team] = league
            games[team] = 0
        return elos[team]

    conn = sqlite3.connect(DB_PATH)
    matches = conn.execute(
        "SELECT gameid, date, league, blue_team, red_team, winner "
        "FROM matches ORDER BY date ASC, gameid ASC"
    ).fetchall()
    conn.close()

    logger.info(f"Processing {len(matches)} matches (half_life={half_life_days}d)…")

    for i, (gameid, date, league, blue, red, winner) in enumerate(matches):
        match_dt = _parse_date(date)

        blue_elo = _get_elo(blue, league)
        red_elo = _get_elo(red, league)

        # Decay toward DEFAULT_ELO based on inactivity
        if blue in last_played:
            days = (match_dt - last_played[blue]).days
            blue_elo = _apply_decay(blue_elo, days, half_life_days)
            elos[blue] = blue_elo
        if red in last_played:
            days = (match_dt - last_played[red]).days
            red_elo = _apply_decay(red_elo, days, half_life_days)
            elos[red] = red_elo

        blue_exp = expected_score(blue_elo, red_elo)
        red_exp = 1.0 - blue_exp

        blue_actual = 1.0 if winner == "blue" else 0.0
        red_actual = 1.0 - blue_actual

        elos[blue] = blue_elo + K * (blue_actual - blue_exp)
        elos[red] = red_elo + K * (red_actual - red_exp)
        games[blue] = games.get(blue, 0) + 1
        games[red] = games.get(red, 0) + 1
        last_played[blue] = match_dt
        last_played[red] = match_dt

        if (i + 1) % 2000 == 0:
            logger.info(f"  Processed {i + 1}/{len(matches)} matches")

    logger.info(f"ELO computed for {len(elos)} teams")
    return {
        team: {"elo": elos[team], "games_played": games[team]}
        for team in elos
    }


# ---------------------------------------------------------------------------
# DB persistence
# ---------------------------------------------------------------------------
def save_to_db(
    elo_results: Dict[str, Dict],
    soloq_elos: Optional[Dict[str, float]] = None,
) -> int:
    """
    Upsert ELO results into the teams table.
    Returns count of teams written.
    """
    conn = sqlite3.connect(DB_PATH)
    now = datetime.now(timezone.utc).isoformat()

    # Look up league for each team from their most recent match
    league_map: Dict[str, str] = {}
    rows = conn.execute(
        "SELECT blue_team, league FROM matches "
        "UNION SELECT red_team, league FROM matches"
    ).fetchall()
    for team, league in rows:
        league_map[team] = league

    written = 0
    for team, data in elo_results.items():
        conn.execute(
            """
            INSERT INTO teams (team_name, pro_elo, games_played, league, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(team_name) DO UPDATE SET
                pro_elo = excluded.pro_elo,
                games_played = excluded.games_played,
                league = excluded.league,
                updated_at = excluded.updated_at
            """,
            (
                team,
                round(data["elo"], 2),
                data["games_played"],
                league_map.get(team, ""),
                now,
            ),
        )
        written += 1

    conn.commit()
    conn.close()
    return written


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------
def main() -> None:
    logger.info("Computing team soloq baselines…")
    soloq_elos = get_team_soloq_elos()
    logger.info(f"  {len(soloq_elos)} teams with soloq baseline")

    logger.info("Running ELO engine over all matches…")
    results = run_elo(soloq_elos)

    n = save_to_db(results, soloq_elos)
    logger.info(f"Saved {n} teams to DB")

    sorted_teams = sorted(results.items(), key=lambda x: x[1]["elo"], reverse=True)
    logger.info("Top 15 teams by ELO:")
    for name, data in sorted_teams[:15]:
        baseline = soloq_elos.get(name)
        tag = f"  (soloq baseline: {baseline:.0f})" if baseline else ""
        logger.info(f"  {name:30} {data['elo']:7.1f}  ({data['games_played']} games){tag}")

    logger.info("Bottom 10 teams by ELO:")
    for name, data in sorted_teams[-10:]:
        logger.info(f"  {name:30} {data['elo']:7.1f}  ({data['games_played']} games)")


if __name__ == "__main__":
    main()
