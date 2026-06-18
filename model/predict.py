"""
Win probability predictor.

Given two teams, computes blended ratings and outputs P(Team A wins).

Run standalone:
  python model/predict.py "Team A" "Team B"
  python model/predict.py --list          # show all teams
"""

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Dict

from loguru import logger

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from model.blend import get_all_ratings, get_team_rating
from model.pro_elo import DEFAULT_ELO

DB_PATH = _ROOT / "db" / "lol_model.db"


# ---------------------------------------------------------------------------
# Core prediction
# ---------------------------------------------------------------------------
def win_probability(rating_a: float, rating_b: float, scale: float = 400.0) -> float:
    """P(A wins) given two ELO-scale ratings."""
    return 1.0 / (1.0 + 10.0 ** (-(rating_a - rating_b) / scale))


def predict_match(
    team_a: str,
    team_b: str,
    blend_k: int = 10,
    scale: float = 400.0,
) -> Dict:
    """
    End-to-end prediction for a matchup.

    Returns dict with team names, blended ratings, and win probabilities.
    Logs a warning if a team isn't in the DB (uses 1500 default).
    """
    rating_a = get_team_rating(team_a, blend_k=blend_k)
    rating_b = get_team_rating(team_b, blend_k=blend_k)

    if rating_a == DEFAULT_ELO:
        logger.warning(f"'{team_a}' not found in teams table — using default {DEFAULT_ELO}")
    if rating_b == DEFAULT_ELO:
        logger.warning(f"'{team_b}' not found in teams table — using default {DEFAULT_ELO}")

    p_a = win_probability(rating_a, rating_b, scale)

    return {
        "team_a": team_a,
        "team_b": team_b,
        "rating_a": round(rating_a, 1),
        "rating_b": round(rating_b, 1),
        "p_a": round(p_a, 4),
        "p_b": round(1.0 - p_a, 4),
    }


def predict_from_ratings(
    rating_a: float,
    rating_b: float,
    scale: float = 400.0,
) -> float:
    """Direct rating-to-probability for use by backtest.py."""
    return win_probability(rating_a, rating_b, scale)


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------
def list_teams() -> None:
    """Print all teams sorted by blended rating."""
    ratings = get_all_ratings()
    if not ratings:
        logger.error("No teams in DB — run pro_elo.py first")
        return

    sorted_teams = sorted(ratings.items(), key=lambda x: x[1], reverse=True)
    print(f"\n{'#':>4}  {'Team':30}  {'Rating':>8}")
    print("-" * 48)
    for i, (team, rating) in enumerate(sorted_teams, 1):
        print(f"{i:4}  {team:30}  {rating:8.1f}")
    print(f"\n{len(sorted_teams)} teams total")


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="LoL T2 win probability predictor")
    parser.add_argument("teams", nargs="*", help="Two team names to predict")
    parser.add_argument("--list", action="store_true", help="List all teams by rating")
    parser.add_argument("--blend-k", type=int, default=10, help="Blend denominator")
    parser.add_argument("--scale", type=float, default=400.0, help="ELO scale factor")
    args = parser.parse_args()

    if args.list:
        list_teams()
        return

    if len(args.teams) != 2:
        parser.error("Provide exactly 2 team names, e.g.: python model/predict.py 'Solary' 'Karmine Corp'")

    result = predict_match(args.teams[0], args.teams[1], blend_k=args.blend_k, scale=args.scale)

    print(f"\n  {result['team_a']}  vs  {result['team_b']}")
    print(f"  Rating: {result['rating_a']:.1f}  vs  {result['rating_b']:.1f}")
    print(f"  Win%:   {result['p_a']*100:.1f}%  vs  {result['p_b']*100:.1f}%\n")


if __name__ == "__main__":
    main()
