from __future__ import annotations

import time
from typing import Optional

import discord
import psutil
from discord import app_commands
from discord.ext import commands

from modules.core.moderator_bot import ModeratorBot
from modules.i18n.strings import locale_string

from .commands.guilds import ban_guild as handle_ban_guild
from .commands.guilds import refresh_guilds as handle_refresh_guilds
from .commands.guilds import unban_guild as handle_unban_guild
from .commands.locale_cmd import send_current_locale
from .commands.metrics import reset_latency
from .commands.stats import build_stats_embed
from .config import DEV_GUILD_ID
from .guards import require_dev_access

__all__ = ["DebugCog", "DEV_GUILD_ID"]


def guild_scope_decorator():
    if DEV_GUILD_ID:
        return app_commands.guilds(discord.Object(id=DEV_GUILD_ID))

    def identity(func):
        return func

    return identity


class DebugCog(commands.Cog):
    def __init__(self, bot: ModeratorBot):
        self.bot = bot
        self.process = psutil.Process()
        self.start_time = time.time()

    @app_commands.command(
        name="stats",
        description=locale_string("cogs.debug.meta.stats.description"),
    )
    @guild_scope_decorator()
    @app_commands.describe(show_all=locale_string("cogs.debug.meta.stats.show_all"))
    @app_commands.checks.has_permissions(administrator=True)
    async def stats(self, interaction: discord.Interaction, show_all: bool = True):
        await interaction.response.defer(ephemeral=True)
        if not await require_dev_access(self.bot, interaction):
            return
        embed = build_stats_embed(self, interaction, show_all)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="reset_averages",
        description="Reset latency averages in the metrics datastore.",
    )
    @guild_scope_decorator()
    @app_commands.describe(
        pattern="Optional Redis pattern to scope rollup keys (defaults to prefix:rollup:*)",
        dry_run="Only show what would be reset without applying changes.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def reset_averages(
        self,
        interaction: discord.Interaction,
        pattern: Optional[str] = None,
        dry_run: bool = False,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await require_dev_access(self.bot, interaction):
            return
        await reset_latency(self, interaction, pattern=pattern, dry_run=dry_run)

    @app_commands.command(
        name="ban_guild",
        description="Restrict a guild from using Moderator Bot.",
    )
    @guild_scope_decorator()
    @app_commands.describe(
        guild_id="The guild ID to ban.",
        reason="Optional reason stored for auditing and owner notification.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def ban_guild(
        self,
        interaction: discord.Interaction,
        guild_id: str,
        reason: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await require_dev_access(self.bot, interaction, denial_key=None):
            return
        await handle_ban_guild(self, interaction, guild_id, reason)

    @app_commands.command(
        name="unban_guild",
        description="Remove a guild from the ban list.",
    )
    @guild_scope_decorator()
    @app_commands.describe(guild_id="The guild ID to unban.")
    @app_commands.checks.has_permissions(administrator=True)
    async def unban_guild(self, interaction: discord.Interaction, guild_id: str) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await require_dev_access(self.bot, interaction, denial_key=None):
            return
        await handle_unban_guild(self, interaction, guild_id)

    @app_commands.command(
        name="refresh_banned_guilds",
        description="Re-run guild synchronisation to enforce banned guilds.",
    )
    @guild_scope_decorator()
    @app_commands.checks.has_permissions(administrator=True)
    async def refresh_banned_guilds(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await require_dev_access(self.bot, interaction, denial_key=None):
            return
        await handle_refresh_guilds(self, interaction)

    @app_commands.command(
        name="locale",
        description=locale_string("cogs.debug.meta.locale.description"),
    )
    async def current_locale(self, interaction: discord.Interaction):
        await send_current_locale(self, interaction)
