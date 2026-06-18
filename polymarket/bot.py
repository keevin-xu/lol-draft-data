"""
Discord bot — continuously scans Polymarket for LoL T2 markets and
sends alerts when +EV opportunities are found.

Setup:
  1. Create a Discord bot at https://discord.com/developers/applications
  2. Add DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID to .env
  3. Invite the bot to your server with Send Messages + Embed Links perms

Run:  python polymarket/bot.py
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Set

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv
from loguru import logger

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

load_dotenv(_ROOT / ".env")

from model.blend import get_all_ratings
from model.predict import predict_match
from polymarket.edge import EdgeSignal, find_edges, format_signal
from polymarket.paper_trader import (
    check_resolutions,
    get_open_positions,
    get_portfolio_summary,
    get_trade_history,
    place_bet,
)
from polymarket.scanner import MarketOpportunity, scan

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SCAN_INTERVAL_MINUTES = 5
MIN_EDGE = 0.03
EDGE_CHANGE_THRESHOLD = 0.02  # re-alert if edge changes by 2%+
PRICE_CHANGE_THRESHOLD = 0.05  # re-alert if price moves $0.05+


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------
class LoLEdgeBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)

        self.tree = app_commands.CommandTree(self)
        self.channel_id = int(os.environ.get("DISCORD_CHANNEL_ID", "0"))
        self.start_time = datetime.now(timezone.utc)
        self.last_scan: Optional[datetime] = None
        self.scan_count = 0
        self.markets_found = 0

        # Track notified signals to avoid spam: market_id → last EdgeSignal
        self._notified: Dict[str, EdgeSignal] = {}

        self._setup_commands()

    def _setup_commands(self) -> None:
        @self.tree.command(name="scan", description="Force an immediate Polymarket scan")
        async def cmd_scan(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            opportunities = scan()
            if not opportunities:
                await interaction.followup.send("No LoL T2 markets currently active on Polymarket.")
                return
            signals = find_edges(opportunities, min_edge=MIN_EDGE)
            if not signals:
                await interaction.followup.send(
                    f"Found {len(opportunities)} markets but no +EV opportunities (edge < {MIN_EDGE:.0%})."
                )
                return
            for sig in signals[:5]:
                embed = self._build_embed(sig)
                await interaction.followup.send(embed=embed)

        @self.tree.command(name="predict", description="Predict a matchup")
        @app_commands.describe(team_a="First team name", team_b="Second team name")
        async def cmd_predict(interaction: discord.Interaction, team_a: str, team_b: str) -> None:
            result = predict_match(team_a, team_b)
            embed = discord.Embed(
                title=f"{result['team_a']} vs {result['team_b']}",
                color=discord.Color.blue(),
            )
            embed.add_field(name="Ratings", value=f"{result['rating_a']:.1f} vs {result['rating_b']:.1f}", inline=False)
            embed.add_field(
                name="Win Probability",
                value=f"**{result['team_a']}**: {result['p_a']:.1%}\n**{result['team_b']}**: {result['p_b']:.1%}",
                inline=False,
            )
            await interaction.response.send_message(embed=embed)

        @self.tree.command(name="leaderboard", description="Top 20 teams by rating")
        async def cmd_leaderboard(interaction: discord.Interaction) -> None:
            ratings = get_all_ratings()
            sorted_teams = sorted(ratings.items(), key=lambda x: x[1], reverse=True)[:20]
            lines = [f"`{i+1:2}. {name:28} {rating:7.1f}`" for i, (name, rating) in enumerate(sorted_teams)]
            embed = discord.Embed(
                title="Top 20 T2 Teams by Blended Rating",
                description="\n".join(lines),
                color=discord.Color.gold(),
            )
            await interaction.response.send_message(embed=embed)

        @self.tree.command(name="status", description="Bot status and scan info")
        async def cmd_status(interaction: discord.Interaction) -> None:
            uptime = datetime.now(timezone.utc) - self.start_time
            hours = int(uptime.total_seconds() // 3600)
            minutes = int((uptime.total_seconds() % 3600) // 60)
            last = self.last_scan.strftime("%H:%M UTC") if self.last_scan else "never"
            summary = get_portfolio_summary()
            embed = discord.Embed(title="Bot Status", color=discord.Color.green())
            embed.add_field(name="Uptime", value=f"{hours}h {minutes}m", inline=True)
            embed.add_field(name="Scans Run", value=str(self.scan_count), inline=True)
            embed.add_field(name="Last Scan", value=last, inline=True)
            embed.add_field(name="Bankroll", value=f"${summary.bankroll:,.2f}", inline=True)
            embed.add_field(name="Paper P&L", value=f"${summary.total_pnl:+,.2f}", inline=True)
            embed.add_field(name="Open Bets", value=str(summary.open_positions), inline=True)
            await interaction.response.send_message(embed=embed)

        @self.tree.command(name="portfolio", description="Paper trading portfolio summary")
        async def cmd_portfolio(interaction: discord.Interaction) -> None:
            s = get_portfolio_summary()
            embed = discord.Embed(title="Paper Trading Portfolio", color=discord.Color.blue())
            embed.add_field(name="Bankroll", value=f"${s.bankroll:,.2f}", inline=True)
            embed.add_field(name="Starting", value=f"$1,000.00", inline=True)
            embed.add_field(name="ROI", value=f"{s.roi:+.1%}", inline=True)
            embed.add_field(name="Total P&L", value=f"${s.total_pnl:+,.2f}", inline=True)
            embed.add_field(name="Win Rate", value=f"{s.win_rate:.0%}" if s.total_bets > 0 else "N/A", inline=True)
            embed.add_field(name="Record", value=f"{s.wins}W / {s.losses}L", inline=True)
            embed.add_field(name="Open Positions", value=str(s.open_positions), inline=True)
            embed.add_field(name="Total Bets", value=str(s.total_bets), inline=True)
            await interaction.response.send_message(embed=embed)

        @self.tree.command(name="trades", description="Recent paper trades")
        async def cmd_trades(interaction: discord.Interaction) -> None:
            open_pos = get_open_positions()
            history = get_trade_history(limit=10)

            lines = []
            if open_pos:
                lines.append("**Open Positions:**")
                for t in open_pos:
                    lines.append(
                        f"`{t.bet_team}` ${t.amount:.2f} @ {t.entry_price:.0%} "
                        f"(edge {t.edge:.1%})"
                    )
            if history:
                lines.append("\n**Recent Settled:**")
                for t in history:
                    icon = "+" if t.status == "won" else "-"
                    lines.append(
                        f"`{icon}` {t.bet_team} ${t.profit_loss:+.2f} "
                        f"(entry {t.entry_price:.0%}, model {t.model_prob:.0%})"
                    )
            if not lines:
                lines.append("No paper trades yet. Waiting for LoL T2 markets on Polymarket.")

            embed = discord.Embed(
                title="Paper Trades",
                description="\n".join(lines),
                color=discord.Color.purple(),
            )
            await interaction.response.send_message(embed=embed)

        @self.tree.command(name="settle", description="Force check for resolved markets")
        async def cmd_settle(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            settled = check_resolutions()
            if not settled:
                await interaction.followup.send("No open positions resolved yet.")
                return
            for t in settled:
                icon = "+" if t.status == "won" else "-"
                pnl = t.profit_loss or 0
                await interaction.followup.send(
                    f"**{icon} Settled:** {t.bet_team} → {t.status.upper()} (${pnl:+.2f})"
                )

    # -----------------------------------------------------------------------
    # Embed builder
    # -----------------------------------------------------------------------
    def _build_embed(self, sig: EdgeSignal) -> discord.Embed:
        opp = sig.opportunity
        bet_team = opp.db_team_a if sig.side == "team_a" else opp.db_team_b

        embed = discord.Embed(
            title=f"+EV: {opp.db_team_a} vs {opp.db_team_b}",
            url=opp.url,
            color=discord.Color.green() if sig.edge >= 0.05 else discord.Color.yellow(),
        )
        embed.add_field(
            name="Model",
            value=f"{opp.db_team_a} **{sig.model_prob_a:.1%}**\n{opp.db_team_b} **{sig.model_prob_b:.1%}**",
            inline=True,
        )
        embed.add_field(
            name="Market",
            value=f"{opp.db_team_a} {opp.market_prob_a:.1%}\n{opp.db_team_b} {opp.market_prob_b:.1%}",
            inline=True,
        )
        embed.add_field(
            name="Signal",
            value=f"Edge: **+{sig.edge:.1%}** on {bet_team}\nKelly: {sig.kelly_fraction:.1%}\nSpread: ${opp.spread:.3f}",
            inline=False,
        )
        embed.set_footer(text=f"Ratings: {sig.rating_a:.0f} vs {sig.rating_b:.0f}")
        return embed

    # -----------------------------------------------------------------------
    # Should we alert on this signal?
    # -----------------------------------------------------------------------
    def _should_alert(self, sig: EdgeSignal) -> bool:
        mid = sig.opportunity.market_id
        prev = self._notified.get(mid)
        if prev is None:
            return True
        edge_change = abs(sig.edge - prev.edge)
        price_change = abs(sig.opportunity.market_prob_a - prev.opportunity.market_prob_a)
        return edge_change >= EDGE_CHANGE_THRESHOLD or price_change >= PRICE_CHANGE_THRESHOLD

    # -----------------------------------------------------------------------
    # Background scan loop
    # -----------------------------------------------------------------------
    @tasks.loop(minutes=SCAN_INTERVAL_MINUTES)
    async def scan_loop(self) -> None:
        try:
            logger.info("Running scheduled scan…")
            opportunities = scan()
            self.scan_count += 1
            self.last_scan = datetime.now(timezone.utc)
            self.markets_found = len(opportunities)

            if not opportunities:
                logger.info("  No LoL markets found")
                return

            signals = find_edges(opportunities, min_edge=MIN_EDGE)
            logger.info(f"  {len(signals)} +EV signals found")

            channel = self.get_channel(self.channel_id)
            if not channel:
                logger.error(f"Channel {self.channel_id} not found — check DISCORD_CHANNEL_ID")
                return

            for sig in signals:
                if self._should_alert(sig):
                    embed = self._build_embed(sig)
                    await channel.send(embed=embed)
                    self._notified[sig.opportunity.market_id] = sig
                    logger.info(f"  Alerted: {sig.opportunity.db_team_a} vs {sig.opportunity.db_team_b} (edge={sig.edge:.1%})")

                    # Paper trade: auto-place bet on new signals
                    trade = place_bet(sig)
                    if trade:
                        await channel.send(
                            f"**Paper bet placed:** ${trade.amount:.2f} on "
                            f"**{trade.bet_team}** @ {trade.entry_price:.0%} "
                            f"(edge: {trade.edge:.1%}, Kelly: {trade.kelly_fraction:.1%})"
                        )

        except Exception as e:
            logger.error(f"Scan loop error: {e}")

    @tasks.loop(hours=1)
    async def settle_loop(self) -> None:
        """Hourly check for resolved markets and settled positions."""
        try:
            settled = check_resolutions()
            if not settled:
                return

            channel = self.get_channel(self.channel_id)
            if not channel:
                return

            for t in settled:
                won = t.status == "won"
                pnl = t.profit_loss or 0
                summary = get_portfolio_summary()
                embed = discord.Embed(
                    title=f"{'WIN' if won else 'LOSS'}: {t.bet_team}",
                    color=discord.Color.green() if won else discord.Color.red(),
                )
                embed.add_field(name="P&L", value=f"${pnl:+.2f}", inline=True)
                embed.add_field(name="Entry", value=f"{t.entry_price:.0%}", inline=True)
                embed.add_field(name="Model", value=f"{t.model_prob:.0%}", inline=True)
                embed.add_field(name="Bankroll", value=f"${summary.bankroll:,.2f}", inline=True)
                embed.add_field(name="Record", value=f"{summary.wins}W / {summary.losses}L", inline=True)
                embed.add_field(name="ROI", value=f"{summary.roi:+.1%}", inline=True)
                await channel.send(embed=embed)

        except Exception as e:
            logger.error(f"Settle loop error: {e}")

    @settle_loop.before_loop
    async def before_settle_loop(self) -> None:
        await self.wait_until_ready()

    @scan_loop.before_loop
    async def before_scan_loop(self) -> None:
        await self.wait_until_ready()

    # -----------------------------------------------------------------------
    # Events
    # -----------------------------------------------------------------------
    async def on_ready(self) -> None:
        logger.info(f"Bot connected as {self.user} (ID: {self.user.id})")

        # Sync slash commands
        try:
            synced = await self.tree.sync()
            logger.info(f"Synced {len(synced)} slash commands")
        except Exception as e:
            logger.error(f"Failed to sync commands: {e}")

        # Start loops
        if not self.scan_loop.is_running():
            self.scan_loop.start()
            logger.info(f"Scan loop started (every {SCAN_INTERVAL_MINUTES} minutes)")
        if not self.settle_loop.is_running():
            self.settle_loop.start()
            logger.info("Settlement loop started (every 1 hour)")

        # Send startup message
        summary = get_portfolio_summary()
        channel = self.get_channel(self.channel_id)
        if channel:
            await channel.send(
                f"**LoL T2 Edge Bot online.** Paper trading enabled.\n"
                f"Bankroll: ${summary.bankroll:,.2f} | "
                f"Record: {summary.wins}W/{summary.losses}L | "
                f"Open: {summary.open_positions}\n"
                f"Commands: `/scan` `/predict` `/portfolio` `/trades` `/settle` `/leaderboard` `/status`"
            )

    async def setup_hook(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    channel_id = os.environ.get("DISCORD_CHANNEL_ID", "").strip()

    if not token:
        logger.error(
            "DISCORD_BOT_TOKEN not set.\n\n"
            "To set up the Discord bot:\n"
            "  1. Go to https://discord.com/developers/applications\n"
            "  2. Create a new application → Bot tab → Reset Token\n"
            "  3. Enable 'Message Content Intent' under Privileged Gateway Intents\n"
            "  4. OAuth2 → URL Generator → check 'bot' + Send Messages/Embed Links\n"
            "  5. Add to .env:\n"
            "     DISCORD_BOT_TOKEN=your_token_here\n"
            "     DISCORD_CHANNEL_ID=your_channel_id\n"
        )
        return

    if not channel_id:
        logger.error(
            "DISCORD_CHANNEL_ID not set.\n"
            "Right-click your Discord channel → Copy Channel ID\n"
            "(Enable Developer Mode in Discord Settings → Advanced first)\n"
        )
        return

    bot = LoLEdgeBot()
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
