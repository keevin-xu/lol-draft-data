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
            embed = discord.Embed(title="Bot Status", color=discord.Color.green())
            embed.add_field(name="Uptime", value=f"{hours}h {minutes}m", inline=True)
            embed.add_field(name="Scans Run", value=str(self.scan_count), inline=True)
            embed.add_field(name="Markets Found", value=str(self.markets_found), inline=True)
            embed.add_field(name="Last Scan", value=last, inline=True)
            embed.add_field(name="Scan Interval", value=f"{SCAN_INTERVAL_MINUTES}m", inline=True)
            embed.add_field(name="Min Edge", value=f"{MIN_EDGE:.0%}", inline=True)
            await interaction.response.send_message(embed=embed)

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

        except Exception as e:
            logger.error(f"Scan loop error: {e}")

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

        # Start scan loop
        if not self.scan_loop.is_running():
            self.scan_loop.start()
            logger.info(f"Scan loop started (every {SCAN_INTERVAL_MINUTES} minutes)")

        # Send startup message
        channel = self.get_channel(self.channel_id)
        if channel:
            await channel.send(
                f"**LoL T2 Edge Bot online.** Scanning Polymarket every {SCAN_INTERVAL_MINUTES} minutes.\n"
                f"Commands: `/scan` `/predict` `/leaderboard` `/status`"
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
