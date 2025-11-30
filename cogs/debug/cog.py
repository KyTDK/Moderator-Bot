from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional
from types import SimpleNamespace

import discord
from discord import app_commands
from discord.ext import commands

from modules.core.health import FeatureStatus, report_feature
from modules.devops import (
    DockerCommandError,
    DockerUpdateConfig,
    DockerUpdateManager,
    UpdateConfigError,
    UpdateReport,
    format_update_report,
)

log = logging.getLogger(__name__)

try:
    import psutil  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - optional dependency guard
    psutil = None
    log.warning("psutil is not installed; debug stats will use limited data.")
    report_feature(
        "core.psutil",
        label="Process metrics",
        status=FeatureStatus.DEGRADED,
        category="core",
        detail="psutil missing; /stats shows limited metrics.",
        remedy="pip install psutil",
        using_fallback=True,
    )

    class _FallbackProcess:
        """Minimal psutil.Process stand-in when psutil is unavailable."""

        def memory_info(self):
            return SimpleNamespace(rss=0, vms=0)

        def cpu_percent(self, interval: float = 0.0) -> float:
            return 0.0

        def num_threads(self) -> int:
            return 0

        def num_handles(self) -> int:
            return 0
else:
    _FallbackProcess = psutil.Process  # type: ignore
    report_feature(
        "core.psutil",
        label="Process metrics",
        status=FeatureStatus.OK,
        category="core",
        detail="psutil active.",
    )

from modules.core.moderator_bot import ModeratorBot
from modules.i18n.strings import locale_string
from modules.utils.log_channel import DeveloperLogField, log_to_developer_channel

from .commands.guilds import ban_guild as handle_ban_guild
from .commands.guilds import refresh_guilds as handle_refresh_guilds
from .commands.guilds import unban_guild as handle_unban_guild
from .commands.locale_cmd import send_current_locale
from .commands.metrics import reset_latency
from .commands.metrics_view import format_latency_breakdown
from .commands.stats import build_stats_embed
from .commands.vectors import (
    VECTOR_STORE_CHOICES,
    report_vector_status,
    reset_vectors as handle_reset_vectors,
)
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
        self.process = psutil.Process() if psutil else _FallbackProcess()
        self.start_time = time.time()
        self._update_lock = asyncio.Lock()

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
        embed = await build_stats_embed(self, interaction, show_all)
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
        name="view_averages",
        description="View latency averages across media types.",
    )
    @guild_scope_decorator()
    @app_commands.checks.has_permissions(administrator=True)
    async def view_averages(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await require_dev_access(self.bot, interaction):
            return
        report = await format_latency_breakdown()
        await interaction.followup.send(report or "No metrics available.", ephemeral=True)

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

    vector_store_choices = [
        app_commands.Choice(name=config.label, value=config.key) for config in VECTOR_STORE_CHOICES
    ]

    @app_commands.command(
        name="reset_vectors",
        description="Delete every vector from an NSFW Milvus collection.",
    )
    @guild_scope_decorator()
    @app_commands.describe(store="Select the vector collection to reset.")
    @app_commands.choices(store=vector_store_choices)
    @app_commands.checks.has_permissions(administrator=True)
    async def reset_vector_store(
        self,
        interaction: discord.Interaction,
        store: app_commands.Choice[str],
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await require_dev_access(self.bot, interaction, denial_key=None):
            return
        await handle_reset_vectors(interaction, store.value)

    @app_commands.command(
        name="vector_status",
        description="View Milvus connection and collection details for NSFW vectors.",
    )
    @guild_scope_decorator()
    @app_commands.checks.has_permissions(administrator=True)
    async def vector_status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await require_dev_access(self.bot, interaction, denial_key=None):
            return
        await report_vector_status(interaction)

    @app_commands.command(
        name="update",
        description="Pull the latest Docker image and redeploy the bot with zero-gap failover.",
    )
    @guild_scope_decorator()
    @app_commands.checks.has_permissions(administrator=True)
    async def update_bot(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await require_dev_access(self.bot, interaction):
            return
        if self._update_lock.locked():
            await interaction.followup.send(
                "Another deployment is already running. Try again once it completes.",
                ephemeral=True,
            )
            return
        try:
            config = DockerUpdateConfig.from_env()
        except UpdateConfigError as exc:
            await interaction.followup.send(f"Update aborted: {exc}", ephemeral=True)
            return

        async with self._update_lock:
            status_message = await interaction.followup.send(
                "Pulling latest Docker image...",
                ephemeral=True,
            )
            manager = DockerUpdateManager(config)
            try:
                report = await manager.run()
            except DockerCommandError as exc:
                failure_message = self._format_update_error(exc)
                await status_message.edit(content=failure_message)
                await self._log_update_failure(interaction.user, config.image, exc)
                return

            summary = format_update_report(report)
            await status_message.edit(content=summary)
            await self._log_update_success(interaction.user, report)

    @app_commands.command(
        name="locale",
        description=locale_string("cogs.debug.meta.locale.description"),
    )
    async def current_locale(self, interaction: discord.Interaction):
        await send_current_locale(self, interaction)

    @staticmethod
    def _format_update_error(error: DockerCommandError) -> str:
        command = " ".join(error.command) or "docker"
        snippet_source = (error.stderr or error.stdout or "").strip()
        if snippet_source:
            snippet = snippet_source.splitlines()[-1]
            if len(snippet) > 200:
                snippet = snippet[:197] + "..."
            detail = f"\nDetails: {snippet}"
        else:
            detail = ""
        return (
            f"Update failed while running `{command}` (exit {error.exit_code})."
            f"{detail}"
        )

    @staticmethod
    def _summarize_output(stdout: str, stderr: str, *, limit: int = 900) -> str:
        payload = (stderr or stdout or "").strip()
        if not payload:
            return "*(no output)*"
        if len(payload) > limit:
            payload = payload[: limit - 3] + "..."
        prefix = "stderr" if stderr else "stdout"
        return f"{prefix}: {payload}"

    async def _log_update_success(self, user: discord.abc.User, report: UpdateReport) -> None:
        user_label = f"{user} ({user.id})"
        fields = [
            DeveloperLogField(name="Triggered by", value=user_label),
            DeveloperLogField(name="Image", value=f"`{report.image}`"),
            DeveloperLogField(name="Rollout", value=report.rollout_mode),
        ]
        for result in report.services:
            fields.append(
                DeveloperLogField(
                    name=f"{result.service} ({result.outcome.duration:.1f}s)",
                    value=self._summarize_output(result.outcome.stdout, result.outcome.stderr),
                )
            )
        await log_to_developer_channel(
            self.bot,
            summary="Zero-gap deployment completed",
            severity="success",
            description=(
                f"Total duration {report.total_duration:.1f}s across {len(report.services)} service(s)."
            ),
            fields=fields,
            context="debug.update",
        )

    async def _log_update_failure(
        self,
        user: discord.abc.User,
        image: str,
        error: DockerCommandError,
    ) -> None:
        user_label = f"{user} ({user.id})"
        fields = [
            DeveloperLogField(name="Triggered by", value=user_label),
            DeveloperLogField(name="Image", value=f"`{image}`"),
            DeveloperLogField(name="Command", value=" ".join(error.command) or "(unknown)"),
            DeveloperLogField(name="Exit code", value=str(error.exit_code)),
            DeveloperLogField(
                name="Output",
                value=self._summarize_output(error.stdout, error.stderr),
            ),
        ]
        await log_to_developer_channel(
            self.bot,
            summary="Deployment failed",
            severity="error",
            description="Docker update command did not complete.",
            fields=fields,
            context="debug.update",
        )
