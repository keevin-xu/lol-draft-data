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

import numpy as np
from loguru import logger

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from model.soloq_rating import get_team_player_ratings

DB_PATH = _ROOT / "db" / "lol_model.db"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
K = 32
DEFAULT_ELO = 1500.0

ROLE_WEIGHTS = {
    "Top": 0.20,
    "Jungle": 0.22,
    "Mid": 0.23,
    "Bot": 0.20,
    "Support": 0.15,
}

# Minimum number of roles with soloq data to compute a team baseline
MIN_ROLES_FOR_BASELINE = 3


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
# ELO engine
# ---------------------------------------------------------------------------
def expected_score(elo_a: float, elo_b: float) -> float:
    return 1.0 / (1.0 + 10.0 ** ((elo_b - elo_a) / 400.0))


def run_elo(
    soloq_elos: Optional[Dict[str, float]] = None,
) -> Dict[str, Dict]:
    """
    Process all matches chronologically and compute ELO for every team.

    Teams with a soloq baseline start at that ELO; others start at 1500.
    Returns {team_name: {"elo": float, "games_played": int}}.
    """
    baselines = soloq_elos or {}
    elos: Dict[str, float] = {}
    games: Dict[str, int] = {}

    def _get_elo(team: str) -> float:
        if team not in elos:
            elos[team] = baselines.get(team, DEFAULT_ELO)
            games[team] = 0
        return elos[team]

    conn = sqlite3.connect(DB_PATH)
    matches = conn.execute(
        "SELECT gameid, date, league, blue_team, red_team, winner "
        "FROM matches ORDER BY date ASC, gameid ASC"
    ).fetchall()
    conn.close()

    logger.info(f"Processing {len(matches)} matches chronologically…")

    for i, (gameid, date, league, blue, red, winner) in enumerate(matches):
        blue_elo = _get_elo(blue)
        red_elo = _get_elo(red)

        blue_exp = expected_score(blue_elo, red_elo)
        red_exp = 1.0 - blue_exp

        blue_actual = 1.0 if winner == "blue" else 0.0
        red_actual = 1.0 - blue_actual

        elos[blue] = blue_elo + K * (blue_actual - blue_exp)
        elos[red] = red_elo + K * (red_actual - red_exp)
        games[blue] = games.get(blue, 0) + 1
        games[red] = games.get(red, 0) + 1

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
