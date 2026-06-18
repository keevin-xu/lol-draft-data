"""
SoloQ rating module — computes individual player strength scores from
rank/LP data and provides team-level soloq aggregation.

Reuses rank_to_rating() from the TTP scraper rather than duplicating
the formula.

Run standalone:  python model/soloq_rating.py
"""

import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from scrapers.ttp_scraper import TIER_BASE, rank_to_rating

DB_PATH = _ROOT / "db" / "lol_model.db"


# ---------------------------------------------------------------------------
# Individual player ratings
# ---------------------------------------------------------------------------
def compute_all_ratings() -> int:
    """
    Batch recompute accounts.soloq_rating from rank_tier + lp.

    Note: the accounts table stores tier but not division, so below-Master
    tiers lose the division offset. This is acceptable because the TTP
    scraper already writes the correct rating (including division) on insert.
    This function serves as a recalculation fallback.

    Returns the count of rows updated.
    """
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT id, rank_tier, lp FROM accounts").fetchall()
    updated = 0
    for row_id, tier, lp in rows:
        tier = tier or "Unranked"
        lp = lp or 0
        rating = rank_to_rating(tier, None, lp)
        conn.execute(
            "UPDATE accounts SET soloq_rating = ? WHERE id = ?",
            (rating, row_id),
        )
        updated += 1
    conn.commit()
    conn.close()
    return updated


def get_player_rating(player_name: str) -> Optional[float]:
    """Return the best (MAX) soloq_rating across a player's accounts."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        """
        SELECT MAX(a.soloq_rating)
        FROM accounts a
        JOIN players p ON a.player_id = p.id
        WHERE p.player_name = ?
          AND a.soloq_rating > 0
        """,
        (player_name,),
    ).fetchone()
    conn.close()
    if row and row[0] is not None:
        return float(row[0])
    return None


# ---------------------------------------------------------------------------
# Team-level soloq aggregation
# ---------------------------------------------------------------------------
_CANONICAL_ROLES = {"Top", "Jungle", "Mid", "Bot", "Support"}


def get_team_player_ratings(
    team: str,
    snapshot_date: Optional[str] = None,
) -> Dict[str, Optional[float]]:
    """
    Look up a team's roster and return {role: best_soloq_rating}.

    Joins rosters → players → accounts. For each role, picks the player
    with the highest soloq_rating (handles subs). Roles with no rated
    player return None.
    """
    conn = sqlite3.connect(DB_PATH)

    date_clause = ""
    params: List = [team]
    if snapshot_date:
        date_clause = "AND r.snapshot_date = ?"
        params.append(snapshot_date)

    rows = conn.execute(
        f"""
        SELECT r.role, r.player_name, COALESCE(MAX(a.soloq_rating), 0) as rating
        FROM rosters r
        LEFT JOIN players p ON LOWER(r.player_name) = LOWER(p.player_name)
        LEFT JOIN accounts a ON a.player_id = p.id
        WHERE r.team = ? {date_clause}
        GROUP BY r.role, r.player_name
        ORDER BY r.role, rating DESC
        """,
        params,
    ).fetchall()
    conn.close()

    result: Dict[str, Optional[float]] = {role: None for role in _CANONICAL_ROLES}
    for role, _name, rating in rows:
        canonical = _normalize_role(role)
        if canonical and canonical in result:
            current = result[canonical]
            if rating > 0 and (current is None or rating > current):
                result[canonical] = float(rating)
    return result


def _normalize_role(role: str) -> Optional[str]:
    """Map Leaguepedia / OE role strings to canonical names."""
    role_lower = role.strip().lower()
    mapping = {
        "top": "Top",
        "jungle": "Jungle",
        "jng": "Jungle",
        "mid": "Mid",
        "bot": "Bot",
        "adc": "Bot",
        "support": "Support",
        "sup": "Support",
    }
    return mapping.get(role_lower)


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------
def main() -> None:
    logger.info("Recomputing all soloq ratings…")
    n = compute_all_ratings()
    logger.info(f"Updated {n} account rows")

    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT rank_tier, soloq_rating FROM accounts"
    ).fetchall()
    conn.close()

    tier_counts = Counter(t for t, _ in rows)
    rated = sum(1 for _, r in rows if r and r > 0)
    ratings = [r for _, r in rows if r and r > 0]

    logger.info(f"Rated: {rated}/{len(rows)}")
    logger.info(
        f"Tier distribution: "
        + " | ".join(f"{k}:{v}" for k, v in sorted(tier_counts.items(), key=lambda x: -x[1]))
    )
    if ratings:
        ratings.sort()
        logger.info(
            f"Rating stats: min={ratings[0]:.0f}  median={ratings[len(ratings)//2]:.0f}"
            f"  max={ratings[-1]:.0f}  mean={sum(ratings)/len(ratings):.0f}"
        )


if __name__ == "__main__":
    main()
