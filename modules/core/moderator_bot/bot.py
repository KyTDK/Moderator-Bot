from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import contextlib
import logging
import os
from typing import Optional

import discord
from discord.ext import commands, tasks

from modules.core.moderator_bot.background import BackgroundTaskMixin
from modules.core.moderator_bot.command_sync import CommandTreeSyncMixin
from modules.core.moderator_bot.connection_health import (
    GatewayHealthMonitor,
    GatewayHealthSnapshot,
)
from modules.core.moderator_bot.network_diagnostics import (
    NetworkDiagnosticAlert,
    NetworkDiagnosticsTask,
)
from modules.i18n.guild_cache import GuildLocaleCache
from modules.i18n.locale_utils import normalise_locale
from modules.i18n.resolution import LocaleResolver
from modules.utils import mysql
from modules.utils.log_channel import log_developer_issue, log_to_developer_channel

_logger = logging.getLogger(__name__)
_DEV_MENTION = os.getenv("DEVELOPER_MENTION", "@here").strip() or None
_RESTART_REASON_GATEWAY = "Gateway disconnect storm"
_RESTART_REASON_NETWORK = "Network probe failures"


@dataclass(slots=True)
class _PendingDeveloperAlert:
    summary: str
    details: str | None
    severity: str
    context: str | None
    attempts: int = 0


class ModeratorBot(
    CommandTreeSyncMixin,
    BackgroundTaskMixin,
    commands.Bot,
):
    def __init__(
        self,
        *,
        instance_id: str,
        heartbeat_seconds: int,
        instance_heartbeat_seconds: int,
        log_cog_loads: bool,
        total_shards: int,
        shard_assignment: Optional[mysql.ShardAssignment] = None,
    ) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.presences = True
        intents.message_content = True
        intents.voice_states = True

        member_cache_flags = discord.MemberCacheFlags.none()
        member_cache_flags.voice = True

        shard_id = shard_assignment.shard_id if shard_assignment else None
        shard_count = (
            shard_assignment.shard_count
            if shard_assignment is not None
            else total_shards
        )

        super().__init__(
            command_prefix=lambda _, __: [],
            intents=intents,
            chunk_guilds_at_startup=False,
            member_cache_flags=member_cache_flags,
            help_command=None,
            max_messages=None,
            shard_id=shard_id,
            shard_count=shard_count,
        )

        self._shard_assignment: Optional[mysql.ShardAssignment] = shard_assignment
        self._instance_id = instance_id
        self._heartbeat_seconds = heartbeat_seconds
        self._instance_heartbeat_seconds = instance_heartbeat_seconds
        self._log_cog_loads = log_cog_loads
        self._standby_login_performed = False
        self._locale_repository = None
        self._translator = None
        self._translation_service = None
        self._command_tree_translator = None
        self._command_tree_translator_loaded = False
        self._guild_locales = GuildLocaleCache()
        self._locale_resolver = LocaleResolver(self._guild_locales)
        self._locale_settings_listener = self._handle_locale_setting_update
        self._command_tree_sync_task = None
        self._command_tree_sync_retry_seconds = 300.0
        self._i18n_bootstrap_task = None
        self._mysql_initialisation_task = None
        self._extension_loader_task = None
        self._guild_cache_preload_task = None
        self._topgg_poster_task = None
        self._guild_sync_task = None
        self._gateway_health = GatewayHealthMonitor(
            threshold=4,
            window_seconds=max(float(heartbeat_seconds), 240.0),
            cooldown_seconds=300.0,
        )
        self._pending_dev_alerts: deque[_PendingDeveloperAlert] = deque()
        self._network_diagnostics = NetworkDiagnosticsTask(
            interval_seconds=45.0,
            failure_threshold=3,
            alert_cooldown_seconds=300.0,
            alert_callback=self._handle_network_alert,
            http_probe=True,
            http_timeout=5.0,
        )
        self._force_restart_task: asyncio.Task[None] | None = None

        mysql.add_settings_listener(self._locale_settings_listener)

        if self._heartbeat_seconds != 60:
            try:
                self.shard_heartbeat.change_interval(seconds=self._heartbeat_seconds)
            except RuntimeError:
                _logger.warning("Failed to adjust heartbeat interval; using default 60s")

        if self._instance_heartbeat_seconds != 5:
            try:
                self.instance_heartbeat.change_interval(
                    seconds=self._instance_heartbeat_seconds
                )
            except RuntimeError:
                _logger.warning(
                    "Failed to adjust instance heartbeat interval; using default 5s"
                )

    async def setup_hook(self) -> None:  # type: ignore[override]
        print("[STARTUP] setup_hook invoked")

        self._ensure_mysql_initialisation_started()
        self._ensure_background_task(
            "_i18n_bootstrap_task",
            lambda: asyncio.create_task(self._initialise_i18n_async()),
        )
        self._ensure_background_task(
            "_extension_loader_task",
            lambda: asyncio.create_task(self._load_extensions_when_ready()),
        )
        self._ensure_background_task(
            "_topgg_poster_task",
            lambda: asyncio.create_task(self._start_topgg_poster_when_ready()),
        )

        self._schedule_command_tree_sync()

        asyncio.create_task(self.push_status("starting"))
        self._network_diagnostics.start()

    async def on_ready(self) -> None:  # type: ignore[override]
        shard_label = (
            f"{self.shard_id}/{self.shard_count}"
            if self.shard_id is not None and self.shard_count is not None
            else "n/a"
        )
        print(
            f"Bot connected as {self.user} in {len(self.guilds)} guilds (shard {shard_label})"
        )

        if self._guild_sync_task is None or self._guild_sync_task.done():
            self._guild_sync_task = asyncio.create_task(self._sync_guilds_with_database())

        asyncio.create_task(self.push_status("ready"))
        asyncio.create_task(self._flush_pending_dev_alerts())

    async def on_resumed(self) -> None:  # type: ignore[override]
        print(">> Gateway session resumed.")
        await self.push_status("ready")
        await self._flush_pending_dev_alerts()

    async def on_disconnect(self) -> None:  # type: ignore[override]
        print(">> Disconnected from gateway.")
        await self.push_status("disconnected")
        await self._handle_gateway_disconnect()

    async def on_connect(self) -> None:  # type: ignore[override]
        print(">> Connected to gateway.")
        await self.push_status("connecting")

    async def on_guild_join(self, guild: discord.Guild) -> None:  # type: ignore[override]
        await self._wait_for_mysql_ready()
        await self.ensure_i18n_ready()

        if await mysql.is_guild_banned(guild.id):
            await self._handle_banned_guild(guild)
            return

        preferred = getattr(guild, "preferred_locale", None)
        normalized_locale = normalise_locale(preferred)
        owner_id = await self._resolve_guild_owner_id(guild)
        if owner_id is None:
            _logger.warning(
                "Skipping guild join sync for %s (%s); owner ID unavailable",
                guild.id,
                guild.name,
            )
            return
        member_count = getattr(guild, "member_count", None)
        if member_count is None:
            member_count = getattr(guild, "approximate_member_count", None)
        await mysql.add_guild(guild.id, guild.name, owner_id, normalized_locale, member_count)
        await self.refresh_guild_locale_override(guild.id)
        dash_url = f"https://modbot.neomechanical.com/dashboard/{guild.id}"

        override = self._guild_locales.get_override(guild.id)
        welcome_message = self.translate(
            "bot.welcome.message",
            locale=override,
            placeholders={"dash_url": dash_url},
        )
        button_label = self.translate(
            "bot.welcome.button_label",
            locale=override,
        )

        view = discord.ui.View()
        view.add_item(
            discord.ui.Button(
                label=button_label,
                url=dash_url,
                emoji="🛠️",
            )
        )

        if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
            try:
                await guild.system_channel.send(welcome_message, view=view)
                return
            except discord.Forbidden:
                pass

        for channel in guild.text_channels:
            if channel.permissions_for(guild.me).send_messages:
                try:
                    await channel.send(welcome_message, view=view)
                    break
                except discord.Forbidden:
                    continue

    async def on_guild_remove(self, guild: discord.Guild) -> None:  # type: ignore[override]
        try:
            await self._wait_for_mysql_ready()
            await mysql.remove_guild(guild.id)
            self._guild_locales.drop(guild.id)
        except asyncio.CancelledError:
            # Shutdown may cancel this event; swallow to avoid “Task was destroyed” spam.
            raise
        except Exception:
            _logger.exception("Failed to process guild remove for %s", guild.id)

    @tasks.loop(hours=6)
    async def cleanup_task(self) -> None:
        await self._wait_for_client_ready()
        guild_ids = [guild.id for guild in self.guilds]
        print(f"[CLEANUP] Running cleanup for {len(guild_ids)} guilds...")

        await mysql.cleanup_orphaned_guilds(guild_ids)
        await mysql.cleanup_expired_strikes()

    @tasks.loop(seconds=60)
    async def shard_heartbeat(self) -> None:
        await self._wait_for_client_ready()
        status = "ready" if self.is_ready() else "starting"
        try:
            await self.push_status(status)
        except Exception:
            _logger.exception("Failed to submit shard heartbeat")

    @tasks.loop(seconds=5)
    async def instance_heartbeat(self) -> None:
        try:
            await mysql.update_instance_heartbeat(self._instance_id)
        except Exception:
            _logger.exception("Failed to submit instance heartbeat")

    async def close(self) -> None:
        if self._command_tree_sync_task is not None:
            self._command_tree_sync_task.cancel()
            try:
                await self._command_tree_sync_task
            except asyncio.CancelledError:
                pass
            finally:
                self._command_tree_sync_task = None

        mysql.remove_settings_listener(self._locale_settings_listener)
        await self._network_diagnostics.stop()
        await super().close()

    async def _handle_gateway_disconnect(self) -> None:
        snapshot = self._gateway_health.record_disconnect()
        if snapshot is None:
            return

        details = self._format_gateway_snapshot(snapshot)
        await self._send_dev_alert(
            summary="Gateway disconnect storm detected",
            details=details,
            severity="critical",
            context="gateway-monitor",
        )
        self._schedule_hard_restart(f"{_RESTART_REASON_GATEWAY}: {details}")

    def _format_gateway_snapshot(self, snapshot: GatewayHealthSnapshot) -> str:
        return (
            f"Observed {snapshot.disconnect_count} disconnects within "
            f"{int(snapshot.window_seconds)}s. First event was "
            f"{snapshot.first_disconnect_age:.1f}s ago. Investigate upstream DNS/network."
        )

    async def _handle_network_alert(self, alert: NetworkDiagnosticAlert) -> None:
        parts = [
            f"Consecutive probe failures: {alert.consecutive_failures}",
            f"Errors: {'; '.join(alert.errors[:5])}",
        ]
        if alert.failure_duration:
            parts.append(f"Failure window: {alert.failure_duration:.1f}s")
        if alert.last_success_age is not None:
            parts.append(f"Last successful lookup: {alert.last_success_age:.1f}s ago")
        if alert.last_success_latency_ms is not None:
            parts.append(f"Last success max latency: {alert.last_success_latency_ms:.1f} ms")

        await self._send_dev_alert(
            summary="DNS resolution failures detected",
            details="\n".join(parts),
            severity="critical",
            context="network-diagnostics",
        )
        if alert.consecutive_failures >= 3:
            self._schedule_hard_restart(
                f"{_RESTART_REASON_NETWORK}: {'; '.join(alert.errors[:3])}"
            )

    async def _send_dev_alert(
        self,
        *,
        summary: str,
        details: str | None,
        severity: str,
        context: str | None,
    ) -> None:
        alert = _PendingDeveloperAlert(summary, details, severity, context)
        if self.is_ready():
            success = await log_developer_issue(
                self,
                summary=summary,
                details=details,
                severity=severity,
                context=context,
                logger=_logger,
            )
            if success:
                return
            alert.attempts += 1
        self._pending_dev_alerts.append(alert)
        _logger.warning("Queued developer alert (%s): %s", severity, summary)

    async def _flush_pending_dev_alerts(self) -> None:
        if not self.is_ready():
            return
        if not self._pending_dev_alerts:
            return

        attempts = len(self._pending_dev_alerts)
        for _ in range(attempts):
            alert = self._pending_dev_alerts.popleft()
            success = await log_developer_issue(
                self,
                summary=alert.summary,
                details=alert.details,
                severity=alert.severity,
                context=alert.context,
                logger=_logger,
            )
            if success:
                continue
            alert.attempts += 1
            if alert.attempts >= 3:
                _logger.warning(
                    "Dropping developer alert after %s failed attempts: %s",
                    alert.attempts,
                    alert.summary,
                )
                continue
            self._pending_dev_alerts.append(alert)
            break

    def _schedule_hard_restart(self, reason: str) -> None:
        """Force a clean process restart via container supervisor after fatal connectivity issues."""
        if self._force_restart_task and not self._force_restart_task.done():
            return

        async def _do_restart() -> None:
            _logger.critical("Triggering forced restart: %s", reason)
            with contextlib.suppress(Exception):
                await self.push_status("restarting")
            with contextlib.suppress(Exception):
                await log_to_developer_channel(
                    self,
                    summary="Restarting process after connectivity failures",
                    severity="critical",
                    description=reason,
                    mention=_DEV_MENTION,
                    context="self-heal",
                )
            loop = asyncio.get_running_loop()
            # Hard-stop safety in case cleanup hangs.
            loop.call_later(5.0, os._exit, 1)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.close(), timeout=3.0)
            os._exit(1)

        self._force_restart_task = asyncio.create_task(
            _do_restart(), name="forced-restart"
        )
