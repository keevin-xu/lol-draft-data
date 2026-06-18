"""
Edge calculator — compares model win probabilities against Polymarket prices
to find +EV betting opportunities.

Run standalone:  python polymarket/edge.py
"""

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from loguru import logger

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from model.predict import predict_match, win_probability
from polymarket.scanner import MarketOpportunity, scan


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class EdgeSignal:
    opportunity: MarketOpportunity
    model_prob_a: float
    model_prob_b: float
    edge: float
    side: str  # "team_a" or "team_b"
    kelly_fraction: float
    rating_a: float
    rating_b: float


# ---------------------------------------------------------------------------
# Edge computation
# ---------------------------------------------------------------------------
MAX_KELLY = 0.25  # cap Kelly at 25% of bankroll


def compute_edge(model_prob: float, market_prob: float) -> float:
    """Edge = model probability - market implied probability."""
    return model_prob - market_prob


def kelly_fraction(model_prob: float, market_prob: float) -> float:
    """
    Simplified Kelly criterion for binary bets.

    f* = (p * b - q) / b
    where p = model_prob, q = 1-p, b = payout odds = (1/market_prob - 1)

    Capped at MAX_KELLY to avoid ruin from model error.
    """
    if market_prob <= 0 or market_prob >= 1:
        return 0.0

    b = (1.0 / market_prob) - 1.0  # decimal odds - 1
    q = 1.0 - model_prob
    f = (model_prob * b - q) / b

    return max(0.0, min(f, MAX_KELLY))


def find_edges(
    opportunities: List[MarketOpportunity],
    min_edge: float = 0.03,
    max_spread: float = 0.15,
) -> List[EdgeSignal]:
    """
    For each market opportunity, run the model and compute edge.

    Filters to opportunities where:
    - Edge exceeds min_edge (default 3%)
    - Market spread is below max_spread (default $0.15)

    Returns list sorted by absolute edge (largest first).
    """
    signals: List[EdgeSignal] = []

    for opp in opportunities:
        # Skip illiquid markets
        if opp.spread > max_spread:
            logger.debug(
                f"  Skipping {opp.db_team_a} vs {opp.db_team_b} — spread too wide (${opp.spread:.3f})"
            )
            continue

        result = predict_match(opp.db_team_a, opp.db_team_b)

        edge_a = compute_edge(result["p_a"], opp.market_prob_a)
        edge_b = compute_edge(result["p_b"], opp.market_prob_b)

        # Take the side with larger edge
        if edge_a >= edge_b and edge_a >= min_edge:
            signals.append(EdgeSignal(
                opportunity=opp,
                model_prob_a=result["p_a"],
                model_prob_b=result["p_b"],
                edge=edge_a,
                side="team_a",
                kelly_fraction=kelly_fraction(result["p_a"], opp.market_prob_a),
                rating_a=result["rating_a"],
                rating_b=result["rating_b"],
            ))
        elif edge_b > edge_a and edge_b >= min_edge:
            signals.append(EdgeSignal(
                opportunity=opp,
                model_prob_a=result["p_a"],
                model_prob_b=result["p_b"],
                edge=edge_b,
                side="team_b",
                kelly_fraction=kelly_fraction(result["p_b"], opp.market_prob_b),
                rating_a=result["rating_a"],
                rating_b=result["rating_b"],
            ))
        else:
            logger.debug(
                f"  No edge on {opp.db_team_a} vs {opp.db_team_b}: "
                f"model={result['p_a']:.1%}/{result['p_b']:.1%} "
                f"market={opp.market_prob_a:.1%}/{opp.market_prob_b:.1%}"
            )

    signals.sort(key=lambda s: s.edge, reverse=True)
    return signals


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def format_signal(sig: EdgeSignal) -> str:
    """Format an edge signal for CLI/Discord output."""
    opp = sig.opportunity
    bet_team = opp.db_team_a if sig.side == "team_a" else opp.db_team_b
    lines = [
        f"Match: {opp.db_team_a} vs {opp.db_team_b}",
        f"  Model:  {opp.db_team_a} {sig.model_prob_a:.1%}  |  {opp.db_team_b} {sig.model_prob_b:.1%}",
        f"  Market: {opp.db_team_a} {opp.market_prob_a:.1%}  |  {opp.db_team_b} {opp.market_prob_b:.1%}",
        f"  Edge:   +{sig.edge:.1%} on {bet_team}",
        f"  Kelly:  {sig.kelly_fraction:.1%} of bankroll",
        f"  Spread: ${opp.spread:.3f}  |  Volume: ${opp.volume:,.0f}",
        f"  {opp.url}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------
def main() -> None:
    opportunities = scan()

    if not opportunities:
        logger.info("No LoL T2 markets currently active on Polymarket.")
        return

    logger.info(f"Checking {len(opportunities)} markets for edge…")
    signals = find_edges(opportunities)

    if not signals:
        logger.info("No +EV opportunities found (edge < 3%).")
        return

    print(f"\n{'='*55}")
    print(f"  +EV OPPORTUNITIES ({len(signals)} found, edge > 3%)")
    print(f"{'='*55}")
    for sig in signals:
        print()
        print(format_signal(sig))
    print()


if __name__ == "__main__":
    main()
