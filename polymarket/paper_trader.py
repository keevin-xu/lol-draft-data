"""
Paper trading engine — tracks hypothetical bets on Polymarket without
placing real orders. Logs positions, checks for resolution, and
computes P&L.

Used by the Discord bot to validate model performance on live markets.
"""

import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests
from loguru import logger

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from polymarket.edge import EdgeSignal
from polymarket.scanner import GAMMA_API, MarketOpportunity, _make_session

DB_PATH = _ROOT / "db" / "lol_model.db"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
STARTING_BANKROLL = 1000.0
MIN_EDGE_TO_BET = 0.03
MAX_POSITION_SIZE = 0.15


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class PaperTrade:
    id: int
    market_id: str
    team_a: str
    team_b: str
    bet_team: str
    side: str
    amount: float
    entry_price: float
    model_prob: float
    edge: float
    kelly_fraction: float
    entry_time: str
    market_url: str
    exit_price: Optional[float]
    match_winner: Optional[str]
    profit_loss: Optional[float]
    status: str


@dataclass
class PortfolioSummary:
    bankroll: float
    total_bets: int
    open_positions: int
    wins: int
    losses: int
    total_pnl: float
    win_rate: float
    roi: float


# ---------------------------------------------------------------------------
# Bankroll management
# ---------------------------------------------------------------------------
def get_current_bankroll() -> float:
    """Current bankroll = starting + sum of all settled P&L."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT COALESCE(SUM(profit_loss), 0) FROM paper_trades WHERE status IN ('won', 'lost')"
    ).fetchone()
    conn.close()
    return STARTING_BANKROLL + float(row[0])


# ---------------------------------------------------------------------------
# Place bets
# ---------------------------------------------------------------------------
def place_bet(signal: EdgeSignal) -> Optional[PaperTrade]:
    """
    Record a paper bet from an EdgeSignal.
    Returns the PaperTrade if placed, None if skipped.
    """
    if signal.edge < MIN_EDGE_TO_BET:
        return None

    opp = signal.opportunity
    bankroll = get_current_bankroll()

    # Size using Kelly, capped at MAX_POSITION_SIZE
    fraction = min(signal.kelly_fraction, MAX_POSITION_SIZE)
    amount = round(bankroll * fraction, 2)

    if amount < 1.0:
        logger.warning(f"Bankroll too low for bet (${bankroll:.2f})")
        return None

    # Don't double-bet the same market
    conn = sqlite3.connect(DB_PATH)
    existing = conn.execute(
        "SELECT id FROM paper_trades WHERE market_id = ? AND status = 'open'",
        (opp.market_id,),
    ).fetchone()
    if existing:
        conn.close()
        return None

    bet_team = opp.db_team_a if signal.side == "team_a" else opp.db_team_b
    entry_price = opp.market_prob_a if signal.side == "team_a" else opp.market_prob_b
    model_prob = signal.model_prob_a if signal.side == "team_a" else signal.model_prob_b
    now = datetime.now(timezone.utc).isoformat()

    conn.execute(
        """
        INSERT INTO paper_trades
            (market_id, team_a, team_b, bet_team, side, amount, entry_price,
             model_prob, edge, kelly_fraction, entry_time, market_url, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
        """,
        (
            opp.market_id, opp.db_team_a, opp.db_team_b, bet_team,
            signal.side, amount, entry_price, model_prob, signal.edge,
            signal.kelly_fraction, now, opp.url,
        ),
    )
    conn.commit()
    trade_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()

    logger.info(
        f"Paper bet placed: ${amount:.2f} on {bet_team} @ {entry_price:.1%} "
        f"(model: {model_prob:.1%}, edge: {signal.edge:.1%})"
    )

    return PaperTrade(
        id=trade_id, market_id=opp.market_id, team_a=opp.db_team_a,
        team_b=opp.db_team_b, bet_team=bet_team, side=signal.side,
        amount=amount, entry_price=entry_price, model_prob=model_prob,
        edge=signal.edge, kelly_fraction=signal.kelly_fraction,
        entry_time=now, market_url=opp.url, exit_price=None,
        match_winner=None, profit_loss=None, status="open",
    )


# ---------------------------------------------------------------------------
# Open positions
# ---------------------------------------------------------------------------
def get_open_positions() -> List[PaperTrade]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT * FROM paper_trades WHERE status = 'open' ORDER BY entry_time DESC"
    ).fetchall()
    conn.close()
    return [_row_to_trade(r) for r in rows]


def get_trade_history(limit: int = 10) -> List[PaperTrade]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT * FROM paper_trades WHERE status IN ('won', 'lost') ORDER BY entry_time DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [_row_to_trade(r) for r in rows]


def _row_to_trade(row: tuple) -> PaperTrade:
    return PaperTrade(
        id=row[0], market_id=row[1], team_a=row[2], team_b=row[3],
        bet_team=row[4], side=row[5], amount=row[6], entry_price=row[7],
        model_prob=row[8], edge=row[9], kelly_fraction=row[10],
        entry_time=row[11], market_url=row[12], exit_price=row[13],
        match_winner=row[14], profit_loss=row[15], status=row[16],
    )


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------
def settle_position(trade: PaperTrade, winner: str) -> float:
    """
    Settle a paper trade. Returns P&L.

    Binary market: buy YES at entry_price.
    Win → shares worth $1.00 → profit = amount * (1/entry_price - 1)
    Lose → shares worth $0.00 → loss = -amount
    """
    bet_won = (winner == trade.bet_team)

    if bet_won:
        pnl = trade.amount * (1.0 / trade.entry_price - 1.0)
        status = "won"
    else:
        pnl = -trade.amount
        status = "lost"

    pnl = round(pnl, 2)

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        UPDATE paper_trades
        SET exit_price = ?, match_winner = ?, profit_loss = ?, status = ?
        WHERE id = ?
        """,
        (1.0 if bet_won else 0.0, winner, pnl, status, trade.id),
    )
    conn.commit()
    conn.close()

    logger.info(
        f"Settled: {trade.bet_team} ({'WON' if bet_won else 'LOST'}) "
        f"→ P&L: ${pnl:+.2f}"
    )
    return pnl


def check_resolutions() -> List[PaperTrade]:
    """
    Check all open positions for resolution.
    Returns list of trades that were settled this call.
    """
    open_trades = get_open_positions()
    if not open_trades:
        return []

    settled: List[PaperTrade] = []
    session = _make_session()

    for trade in open_trades:
        winner = _check_polymarket_resolution(trade, session)
        if not winner:
            winner = _check_matches_table(trade)
        if winner:
            settle_position(trade, winner)
            trade.status = "won" if winner == trade.bet_team else "lost"
            trade.match_winner = winner
            settled.append(trade)

    return settled


def _check_polymarket_resolution(trade: PaperTrade, session: requests.Session) -> Optional[str]:
    """Check Polymarket API for market resolution."""
    try:
        r = session.get(
            f"{GAMMA_API}/markets/{trade.market_id}",
            timeout=10,
        )
        if r.status_code != 200:
            return None
        market = r.json()
        if not market.get("closed") or not market.get("resolvedBy"):
            return None

        prices = market.get("outcomePrices", [])
        if len(prices) >= 2:
            if prices[0] == "1":
                return trade.team_a
            elif prices[1] == "1":
                return trade.team_b
    except Exception as e:
        logger.debug(f"Polymarket resolution check failed: {e}")
    return None


def _check_matches_table(trade: PaperTrade) -> Optional[str]:
    """Check our matches DB for a result matching this trade."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        """
        SELECT blue_team, red_team, winner FROM matches
        WHERE ((blue_team = ? AND red_team = ?) OR (blue_team = ? AND red_team = ?))
          AND date > ?
        ORDER BY date ASC LIMIT 1
        """,
        (trade.team_a, trade.team_b, trade.team_b, trade.team_a, trade.entry_time),
    ).fetchone()
    conn.close()

    if not row:
        return None

    blue, red, winner_side = row
    if winner_side == "blue":
        return blue
    elif winner_side == "red":
        return red
    return None


# ---------------------------------------------------------------------------
# Portfolio summary
# ---------------------------------------------------------------------------
def get_portfolio_summary() -> PortfolioSummary:
    conn = sqlite3.connect(DB_PATH)

    total_pnl = conn.execute(
        "SELECT COALESCE(SUM(profit_loss), 0) FROM paper_trades WHERE status IN ('won', 'lost')"
    ).fetchone()[0]
    wins = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE status = 'won'").fetchone()[0]
    losses = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE status = 'lost'").fetchone()[0]
    open_pos = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE status = 'open'").fetchone()[0]
    total_bets = wins + losses

    conn.close()

    bankroll = STARTING_BANKROLL + float(total_pnl)
    win_rate = wins / total_bets if total_bets > 0 else 0.0
    roi = float(total_pnl) / STARTING_BANKROLL if STARTING_BANKROLL > 0 else 0.0

    return PortfolioSummary(
        bankroll=bankroll,
        total_bets=total_bets,
        open_positions=open_pos,
        wins=wins,
        losses=losses,
        total_pnl=float(total_pnl),
        win_rate=win_rate,
        roi=roi,
    )
