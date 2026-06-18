"""
Walk-forward backtester for the LoL T2 prediction model.

Processes matches chronologically — predicts BEFORE seeing the result,
updates ELO AFTER. No lookahead, no data leakage.

Run:
  python backtest/backtest.py                # default params, full report
  python backtest/backtest.py --optimize     # grid search over K/blend_k/scale
  python backtest/backtest.py --league LCKC  # backtest single league
"""

import argparse
import math
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from loguru import logger

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from model.pro_elo import (
    DEFAULT_ELO,
    HALF_LIFE_DAYS,
    LEAGUE_TO_REGION,
    _apply_decay,
    _parse_date,
    compute_regional_offsets,
    expected_score,
    get_team_soloq_elos,
)

DB_PATH = _ROOT / "db" / "lol_model.db"

WARMUP_MONTHS = 3  # process this many months before logging predictions


# ---------------------------------------------------------------------------
# Incremental ELO tracker
# ---------------------------------------------------------------------------
class ELOTracker:
    """
    Stateful ELO tracker for walk-forward backtesting.
    Predict BEFORE result, update AFTER. Applies time-decay between matches.
    """

    def __init__(
        self,
        K: float = 48,
        blend_k: int = 5,
        scale: float = 500.0,
        half_life_days: float = HALF_LIFE_DAYS,
        soloq_baselines: Optional[Dict[str, float]] = None,
        regional_offsets: Optional[Dict[str, float]] = None,
    ) -> None:
        self.K = K
        self.blend_k = blend_k
        self.scale = scale
        self.half_life_days = half_life_days
        self.elos: Dict[str, float] = {}
        self.games: Dict[str, int] = {}
        self.last_played: Dict[str, datetime] = {}
        self.soloq_baselines = soloq_baselines or {}
        self.regional_offsets = regional_offsets or {}

    def _init_team(self, team: str, league: str) -> None:
        if team in self.elos:
            return
        if team in self.soloq_baselines:
            self.elos[team] = self.soloq_baselines[team]
        else:
            region = LEAGUE_TO_REGION.get(league, "")
            offset = self.regional_offsets.get(region, 0.0)
            self.elos[team] = DEFAULT_ELO + offset
        self.games[team] = 0

    def _decay_team(self, team: str, match_dt: datetime) -> None:
        if team in self.last_played:
            days = (match_dt - self.last_played[team]).days
            self.elos[team] = _apply_decay(self.elos[team], days, self.half_life_days)

    def get_blended_rating(self, team: str, league: str) -> float:
        self._init_team(team, league)
        pro_elo = self.elos[team]
        soloq_elo = self.soloq_baselines.get(team, DEFAULT_ELO)
        gp = self.games[team]
        alpha = gp / (gp + self.blend_k)
        return alpha * pro_elo + (1.0 - alpha) * soloq_elo

    def predict(self, blue: str, red: str, league: str, date: str) -> float:
        """Return P(blue wins) without updating state."""
        self._init_team(blue, league)
        self._init_team(red, league)

        match_dt = _parse_date(date)
        blue_elo = _apply_decay(
            self.elos[blue],
            (match_dt - self.last_played[blue]).days if blue in self.last_played else 0,
            self.half_life_days,
        )
        red_elo = _apply_decay(
            self.elos[red],
            (match_dt - self.last_played[red]).days if red in self.last_played else 0,
            self.half_life_days,
        )

        soloq_blue = self.soloq_baselines.get(blue, DEFAULT_ELO)
        soloq_red = self.soloq_baselines.get(red, DEFAULT_ELO)
        gp_blue = self.games.get(blue, 0)
        gp_red = self.games.get(red, 0)
        alpha_blue = gp_blue / (gp_blue + self.blend_k)
        alpha_red = gp_red / (gp_red + self.blend_k)

        rating_a = alpha_blue * blue_elo + (1.0 - alpha_blue) * soloq_blue
        rating_b = alpha_red * red_elo + (1.0 - alpha_red) * soloq_red

        return 1.0 / (1.0 + 10.0 ** (-(rating_a - rating_b) / self.scale))

    def update(self, blue: str, red: str, winner: str, league: str, date: str) -> None:
        """Update ELOs after seeing result, with time decay."""
        self._init_team(blue, league)
        self._init_team(red, league)

        match_dt = _parse_date(date)

        self._decay_team(blue, match_dt)
        self._decay_team(red, match_dt)

        blue_elo = self.elos[blue]
        red_elo = self.elos[red]

        blue_exp = expected_score(blue_elo, red_elo)
        blue_actual = 1.0 if winner == "blue" else 0.0

        self.elos[blue] = blue_elo + self.K * (blue_actual - blue_exp)
        self.elos[red] = red_elo + self.K * ((1.0 - blue_actual) - (1.0 - blue_exp))
        self.games[blue] = self.games.get(blue, 0) + 1
        self.games[red] = self.games.get(red, 0) + 1
        self.last_played[blue] = match_dt
        self.last_played[red] = match_dt


# ---------------------------------------------------------------------------
# Backtest result
# ---------------------------------------------------------------------------
@dataclass
class BacktestResult:
    K: float
    blend_k: int
    scale: float
    half_life: float = HALF_LIFE_DAYS
    warmup_matches: int = 0
    test_matches: int = 0
    correct: int = 0
    accuracy: float = 0.0
    brier_score: float = 0.0
    log_loss: float = 0.0
    calibration: Dict[str, Tuple[float, float, int]] = field(default_factory=dict)
    league_accuracy: Dict[str, Tuple[int, int]] = field(default_factory=dict)
    predictions: List[Tuple[float, float]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Core backtest
# ---------------------------------------------------------------------------
def load_matches(league_filter: Optional[str] = None) -> List[Tuple]:
    conn = sqlite3.connect(DB_PATH)
    query = "SELECT gameid, date, league, blue_team, red_team, winner FROM matches ORDER BY date ASC, gameid ASC"
    if league_filter:
        query = f"SELECT gameid, date, league, blue_team, red_team, winner FROM matches WHERE league = ? ORDER BY date ASC, gameid ASC"
        rows = conn.execute(query, (league_filter,)).fetchall()
    else:
        rows = conn.execute(query).fetchall()
    conn.close()
    return rows


def run_backtest(
    K: float = 48,
    blend_k: int = 5,
    scale: float = 500.0,
    half_life_days: float = HALF_LIFE_DAYS,
    warmup_months: int = WARMUP_MONTHS,
    league_filter: Optional[str] = None,
    verbose: bool = False,
) -> BacktestResult:
    """
    Run walk-forward backtest over all matches.

    Returns BacktestResult with all metrics.
    """
    soloq_baselines = get_team_soloq_elos()
    regional_offsets = compute_regional_offsets()

    tracker = ELOTracker(
        K=K,
        blend_k=blend_k,
        scale=scale,
        half_life_days=half_life_days,
        soloq_baselines=soloq_baselines,
        regional_offsets=regional_offsets,
    )

    matches = load_matches(league_filter)
    if not matches:
        logger.error("No matches found")
        return BacktestResult(K=K, blend_k=blend_k, scale=scale, half_life=half_life_days)

    # Determine warmup cutoff date
    first_date = matches[0][1]
    warmup_year = int(first_date[:4])
    warmup_month = int(first_date[5:7]) + warmup_months
    while warmup_month > 12:
        warmup_month -= 12
        warmup_year += 1
    warmup_cutoff = f"{warmup_year:04d}-{warmup_month:02d}-01"

    # Walk-forward
    result = BacktestResult(K=K, blend_k=blend_k, scale=scale, half_life=half_life_days)
    predictions: List[Tuple[float, float]] = []
    league_stats: Dict[str, List[int]] = defaultdict(lambda: [0, 0])

    for gameid, date, league, blue, red, winner in matches:
        if date < warmup_cutoff:
            tracker.update(blue, red, winner, league, date)
            result.warmup_matches += 1
            continue

        # Predict BEFORE seeing result
        p_blue = tracker.predict(blue, red, league, date)
        actual = 1.0 if winner == "blue" else 0.0

        predictions.append((p_blue, actual))

        # Track accuracy
        predicted_winner = p_blue >= 0.5
        actual_winner = actual == 1.0
        if predicted_winner == actual_winner:
            result.correct += 1
        league_stats[league][0] += 1  # total
        if predicted_winner == actual_winner:
            league_stats[league][1] += 1  # correct

        # Update AFTER seeing result
        tracker.update(blue, red, winner, league, date)

    result.test_matches = len(predictions)
    result.predictions = predictions

    if not predictions:
        return result

    # Compute metrics
    preds = np.array([p for p, _ in predictions])
    actuals = np.array([a for _, a in predictions])

    result.accuracy = result.correct / result.test_matches

    # Brier score: mean(( p - actual )^2)
    result.brier_score = float(np.mean((preds - actuals) ** 2))

    # Log loss: -mean(actual*log(p) + (1-actual)*log(1-p))
    eps = 1e-10
    preds_clipped = np.clip(preds, eps, 1.0 - eps)
    result.log_loss = float(-np.mean(
        actuals * np.log(preds_clipped) + (1 - actuals) * np.log(1 - preds_clipped)
    ))

    # Calibration: bin predictions into buckets
    bins = [(0.50, 0.55), (0.55, 0.60), (0.60, 0.65), (0.65, 0.70),
            (0.70, 0.75), (0.75, 0.80), (0.80, 0.85), (0.85, 0.90), (0.90, 1.01)]
    for lo, hi in bins:
        # Use the "favorite" probability (always >= 0.5)
        mask = []
        fav_actuals = []
        for p, a in predictions:
            fav_p = max(p, 1.0 - p)
            fav_a = a if p >= 0.5 else 1.0 - a
            if lo <= fav_p < hi:
                mask.append(fav_p)
                fav_actuals.append(fav_a)
        if mask:
            avg_pred = sum(mask) / len(mask)
            avg_actual = sum(fav_actuals) / len(fav_actuals)
            label = f"{lo:.0%}-{hi:.0%}"
            result.calibration[label] = (avg_pred, avg_actual, len(mask))

    # Per-league accuracy
    result.league_accuracy = {
        league: (correct, total)
        for league, (total, correct) in league_stats.items()
    }

    return result


# ---------------------------------------------------------------------------
# Grid search
# ---------------------------------------------------------------------------
def grid_search(
    k_values: List[float] = None,
    blend_k_values: List[int] = None,
    scale_values: List[float] = None,
    half_life_values: List[float] = None,
    league_filter: Optional[str] = None,
) -> List[BacktestResult]:
    """
    Sweep parameter combinations. Returns results sorted by Brier score (best first).
    """
    k_values = k_values or [32, 48, 64]
    blend_k_values = blend_k_values or [5, 10, 20]
    scale_values = scale_values or [400, 500]
    half_life_values = half_life_values or [90, 180, 270, 365, 9999]

    total = len(k_values) * len(blend_k_values) * len(scale_values) * len(half_life_values)
    logger.info(f"Grid search: {total} combinations")

    results: List[BacktestResult] = []
    done = 0

    for k in k_values:
        for bk in blend_k_values:
            for sc in scale_values:
                for hl in half_life_values:
                    r = run_backtest(K=k, blend_k=bk, scale=sc, half_life_days=hl, league_filter=league_filter)
                    results.append(r)
                    done += 1
                    if done % 20 == 0:
                        logger.info(f"  {done}/{total} completed")

    results.sort(key=lambda r: r.brier_score)
    return results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def print_report(result: BacktestResult) -> None:
    print(f"\n{'='*60}")
    print(f"  BACKTEST REPORT")
    print(f"{'='*60}")
    hl_str = "off" if result.half_life >= 9000 else f"{result.half_life:.0f}d"
    print(f"  Parameters: K={result.K}, blend_k={result.blend_k}, scale={result.scale}, half_life={hl_str}")
    print(f"  Warmup:     {result.warmup_matches} matches")
    print(f"  Test:       {result.test_matches} matches")
    print()
    print(f"  Accuracy:    {result.accuracy:.1%}  ({result.correct}/{result.test_matches})")
    print(f"  Brier Score: {result.brier_score:.4f}  (lower=better, random=0.2500)")
    print(f"  Log Loss:    {result.log_loss:.4f}  (lower=better, random=0.6931)")
    print()

    # Calibration table
    print(f"  {'Bucket':12} {'Predicted':>10} {'Actual':>10} {'Count':>8}")
    print(f"  {'-'*42}")
    for label, (pred, actual, count) in sorted(result.calibration.items()):
        diff = actual - pred
        arrow = "+" if diff > 0.02 else ("-" if diff < -0.02 else " ")
        print(f"  {label:12} {pred:10.1%} {actual:10.1%} {count:8d}  {arrow}")
    print()

    # Per-league breakdown
    league_data = sorted(result.league_accuracy.items(), key=lambda x: x[1][0] / max(x[1][1], 1), reverse=True)
    print(f"  {'League':10} {'Accuracy':>10} {'Correct':>8} {'Total':>8}")
    print(f"  {'-'*38}")
    for league, (correct, total) in league_data:
        acc = correct / total if total > 0 else 0
        print(f"  {league:10} {acc:10.1%} {correct:8d} {total:8d}")
    print()


def print_grid_results(results: List[BacktestResult], top_n: int = 15) -> None:
    print(f"\n{'='*85}")
    print(f"  GRID SEARCH RESULTS (top {top_n} of {len(results)} by Brier Score)")
    print(f"{'='*85}")
    print(f"  {'#':>3}  {'K':>4}  {'blend_k':>7}  {'scale':>6}  {'half_life':>9}  {'Brier':>8}  {'LogLoss':>8}  {'Accuracy':>9}")
    print(f"  {'-'*65}")
    for i, r in enumerate(results[:top_n]):
        hl = "off" if r.half_life >= 9000 else f"{r.half_life:.0f}d"
        print(f"  {i+1:3}  {r.K:4.0f}  {r.blend_k:7d}  {r.scale:6.0f}  {hl:>9}  {r.brier_score:8.4f}  {r.log_loss:8.4f}  {r.accuracy:9.1%}")

    print(f"\n  Worst:")
    worst = results[-1]
    hl = "off" if worst.half_life >= 9000 else f"{worst.half_life:.0f}d"
    print(f"       {worst.K:4.0f}  {worst.blend_k:7d}  {worst.scale:6.0f}  {hl:>9}  {worst.brier_score:8.4f}  {worst.log_loss:8.4f}  {worst.accuracy:9.1%}")
    print()

    best = results[0]
    hl = "off" if best.half_life >= 9000 else f"{best.half_life:.0f}d"
    print(f"  BEST: K={best.K:.0f}, blend_k={best.blend_k}, scale={best.scale}, half_life={hl}")
    print(f"    Brier={best.brier_score:.4f}  LogLoss={best.log_loss:.4f}  Accuracy={best.accuracy:.1%}")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="LoL T2 model backtester")
    parser.add_argument("--optimize", action="store_true", help="Run grid search over K/blend_k/scale/half_life")
    parser.add_argument("--league", type=str, default=None, help="Filter to a single league")
    parser.add_argument("--K", type=float, default=48, help="ELO K-factor")
    parser.add_argument("--blend-k", type=int, default=5, help="Blend denominator")
    parser.add_argument("--scale", type=float, default=500, help="ELO scale factor")
    parser.add_argument("--half-life", type=float, default=HALF_LIFE_DAYS, help="ELO decay half-life in days (9999=off)")
    args = parser.parse_args()

    if args.optimize:
        logger.info("Starting parameter optimization…")
        results = grid_search(league_filter=args.league)
        print_grid_results(results)

        best = results[0]
        hl = "off" if best.half_life >= 9000 else f"{best.half_life:.0f}d"
        logger.info(f"Running detailed report with best params: K={best.K}, blend_k={best.blend_k}, scale={best.scale}, half_life={hl}")
        detailed = run_backtest(K=best.K, blend_k=best.blend_k, scale=best.scale, half_life_days=best.half_life, league_filter=args.league)
        print_report(detailed)
    else:
        hl = "off" if args.half_life >= 9000 else f"{args.half_life:.0f}d"
        logger.info(f"Running backtest (K={args.K}, blend_k={args.blend_k}, scale={args.scale}, half_life={hl})…")
        result = run_backtest(K=args.K, blend_k=args.blend_k, scale=args.scale, half_life_days=args.half_life, league_filter=args.league)
        print_report(result)


if __name__ == "__main__":
    main()
