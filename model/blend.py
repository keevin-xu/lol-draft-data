"""
Dynamic alpha blending of Pro ELO and SoloQ baseline ratings.

Teams with more pro matches get weighted toward pro ELO; new teams
lean on their soloq baseline. The blend denominator (blend_k) is
a tunable hyperparameter — the backtest will optimize it.

Does NOT write to DB — blended ratings are computed on-the-fly.

Run standalone:  python model/blend.py
"""

import sqlite3
import sys
from pathlib import Path
from typing import Dict, Optional

from loguru import logger

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from model.pro_elo import DEFAULT_ELO, get_league_offset, get_team_soloq_elos

DB_PATH = _ROOT / "db" / "lol_model.db"


# ---------------------------------------------------------------------------
# Core blending
# ---------------------------------------------------------------------------
def compute_blended_rating(
    pro_elo: float,
    soloq_elo: float,
    games_played: int,
    blend_k: int = 5,
) -> float:
    """
    Pure function. Blend pro ELO and soloq baseline.

    alpha = games_played / (games_played + blend_k)
      - 0 games  → alpha=0.00 → 100% soloq
      - 10 games → alpha=0.50 → 50/50
      - 30 games → alpha=0.75 → 75% pro
    """
    alpha = games_played / (games_played + blend_k)
    return alpha * pro_elo + (1.0 - alpha) * soloq_elo


# ---------------------------------------------------------------------------
# DB-backed lookups
# ---------------------------------------------------------------------------
def get_team_rating(team_name: str, blend_k: int = 5) -> float:
    """
    Return the blended rating for a single team.

    Falls back to DEFAULT_ELO if the team isn't in the teams table.
    """
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT pro_elo, games_played, league FROM teams WHERE team_name = ?",
        (team_name,),
    ).fetchone()
    conn.close()

    if row is None:
        return DEFAULT_ELO

    pro_elo, games_played, league = float(row[0]), int(row[1]), row[2] or ""
    soloq_elos = get_team_soloq_elos()
    soloq_elo = soloq_elos.get(team_name, DEFAULT_ELO + get_league_offset(league))

    return compute_blended_rating(pro_elo, soloq_elo, games_played, blend_k)


def get_all_ratings(blend_k: int = 5) -> Dict[str, float]:
    """
    Return blended ratings for every team in the teams table.
    """
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT team_name, pro_elo, games_played, league FROM teams"
    ).fetchall()
    conn.close()

    soloq_elos = get_team_soloq_elos()

    return {
        team: compute_blended_rating(
            float(pro_elo),
            soloq_elos.get(team, DEFAULT_ELO + get_league_offset(league or "")),
            int(gp),
            blend_k,
        )
        for team, pro_elo, gp, league in rows
    }


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------
def main() -> None:
    ratings = get_all_ratings()
    if not ratings:
        logger.error("No teams in DB — run pro_elo.py first")
        return

    sorted_teams = sorted(ratings.items(), key=lambda x: x[1], reverse=True)
    soloq_elos = get_team_soloq_elos()

    conn = sqlite3.connect(DB_PATH)
    team_data = {
        row[0]: (float(row[1]), int(row[2]))
        for row in conn.execute(
            "SELECT team_name, pro_elo, games_played FROM teams"
        ).fetchall()
    }
    conn.close()

    logger.info(f"Blended ratings for {len(sorted_teams)} teams (blend_k=10):")
    for name, blended in sorted_teams[:20]:
        pro_elo, gp = team_data.get(name, (DEFAULT_ELO, 0))
        alpha = gp / (gp + 10)
        soloq = soloq_elos.get(name)
        sq_str = f"soloq={soloq:.0f}" if soloq else "no soloq"
        logger.info(
            f"  {name:30}  blended={blended:7.1f}  "
            f"pro={pro_elo:7.1f}  {sq_str:16}  "
            f"α={alpha:.2f}  ({gp} games)"
        )


if __name__ == "__main__":
    main()
