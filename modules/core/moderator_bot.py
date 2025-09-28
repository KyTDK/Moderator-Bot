from __future__ import annotations

import logging
import os
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

import discord
from discord.ext import commands, tasks

from modules.post_stats.topgg_poster import start_topgg_poster
from modules.utils import mysql
from modules.i18n import (
    CrowdinConfigurationError,
    CrowdinSettings,
    CrowdinTranslationService,
    LocaleRepository,
    Translator,
)

_logger = logging.getLogger(__name__)

class ModeratorBot(commands.Bot):
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
        self._crowdin_service: CrowdinTranslationService | None = None
        self._locale_repository: LocaleRepository | None = None
        self._translator: Translator | None = None
        self._initialise_i18n()

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

    def _initialise_i18n(self) -> None:
        default_locale = os.getenv("I18N_DEFAULT_LOCALE", "en")
        fallback_locale = os.getenv("I18N_FALLBACK_LOCALE") or default_locale
        locales_override = os.getenv("I18N_LOCALES_DIR")

        try:
            settings = CrowdinSettings.from_env()
        except CrowdinConfigurationError:
            settings = None
            _logger.info("Crowdin integration disabled; missing configuration.")
        else:
            if locales_override:
                settings = replace(
                    settings,
                    locales_root=Path(locales_override).resolve(),
                )
            self._crowdin_service = CrowdinTranslationService(settings)

        if self._crowdin_service is not None:
            locales_root = self._crowdin_service.settings.locales_root
        else:
            base_root = locales_override or os.getenv("CROWDIN_LOCALES_DIR") or os.getenv("LOCALES_DIR") or "locales"
            locales_root = Path(base_root).resolve()

        self._locale_repository = LocaleRepository(
            locales_root,
            default_locale=default_locale,
            fallback_locale=fallback_locale,
        )

        auto_sync = os.getenv("CROWDIN_SYNC_ON_STARTUP", "false").lower() in {"1", "true", "yes", "on"}
        if auto_sync and self._crowdin_service is not None:
            try:
                self._locale_repository.refresh(self._crowdin_service)
            except Exception:
                _logger.exception("Failed to refresh translations from Crowdin on startup")
                self._locale_repository.reload()
        else:
            self._locale_repository.ensure_loaded()

        self._translator = Translator(self._locale_repository)

    @property
    def translator(self) -> Translator:
        if self._translator is None:
            raise RuntimeError("Translator has not been initialised")
        return self._translator

    @property
    def locale_repository(self) -> LocaleRepository:
        if self._locale_repository is None:
            raise RuntimeError("Locale repository has not been initialised")
        return self._locale_repository

    @property
    def crowdin_service(self) -> CrowdinTranslationService | None:
        return self._crowdin_service

    async def refresh_translations(self, *, fetch: bool = True) -> None:
        if self._locale_repository is None:
            _logger.warning("Translation refresh requested but no locale repository is configured")
            return

        if fetch and self._crowdin_service is not None:
            await self._locale_repository.refresh_async(self._crowdin_service)
            _logger.info("Translations refreshed from Crowdin")
        else:
            if fetch and self._crowdin_service is None:
                _logger.warning("Crowdin service unavailable; reloading translations from disk")
            await self._locale_repository.reload_async()
            _logger.info("Translations reloaded from disk")

    def translate(
        self,
        key: str,
        *,
        locale: str | None = None,
        placeholders: dict[str, Any] | None = None,
        fallback: str | None = None,
    ) -> Any:
        translator = self._translator
        if translator is None:
            return fallback if fallback is not None else key
        return translator.translate(
            key,
            locale=locale,
            placeholders=placeholders,
            fallback=fallback,
        )

    def set_shard_assignment(self, shard_assignment: mysql.ShardAssignment) -> None:
        """Attach a shard assignment to the bot (used for standby takeover)."""

        self._shard_assignment = shard_assignment
        self.shard_id = shard_assignment.shard_id
        self.shard_count = shard_assignment.shard_count
        self._connection.shard_id = shard_assignment.shard_id
        self._connection.shard_count = shard_assignment.shard_count

    async def prepare_standby(self, token: str) -> None:
        """Log in without connecting so the bot is ready for a fast takeover."""

        if self._standby_login_performed:
            return

        await self.login(token)
        self._standby_login_performed = True

    async def push_status(self, status: str, *, last_error: str | None = None) -> None:
        if self._shard_assignment is None:
            return
        ws = getattr(self, "ws", None)
        session_id = getattr(ws, "session_id", None)
        resume_url = getattr(ws, "resume_url", None)
        try:
            await mysql.update_shard_status(
                shard_id=self._shard_assignment.shard_id,
                instance_id=self._instance_id,
                status=status,
                session_id=session_id,
                resume_url=resume_url,
                last_error=last_error,
            )
        except Exception:
            _logger.exception("Failed to update shard status (%s)", status)

    async def setup_hook(self) -> None:  # type: ignore[override]
        try:
            await mysql.initialise_and_get_pool()
        except Exception as exc:
            print(f"[FATAL] MySQL init failed: {exc}")
            raise

        await mysql.update_instance_heartbeat(self._instance_id)

        for loop_task in (self.cleanup_task, self.shard_heartbeat, self.instance_heartbeat):
            if not loop_task.is_running():
                try:
                    loop_task.start()
                except RuntimeError:
                    pass

        await self._load_extensions()

        try:
            start_topgg_poster(self)
        except Exception as exc:
            print(f"[WARN] top.gg poster could not start: {exc}")

        try:
            await self.tree.sync(guild=None)
        except Exception as exc:
            print(f"[ERROR] Command tree sync failed: {exc}")

        await self.push_status("starting")

    async def on_ready(self) -> None:  # type: ignore[override]
        shard_label = (
            f"{self.shard_id}/{self.shard_count}"
            if self.shard_id is not None and self.shard_count is not None
            else "n/a"
        )
        print(
            f"Bot connected as {self.user} in {len(self.guilds)} guilds (shard {shard_label})"
        )

        for guild in self.guilds:
            try:
                await mysql.add_guild(guild.id, guild.name, guild.owner_id)
            except Exception as exc:
                print(f"[ERROR] Failed to sync guild {guild.id}: {exc}")
        print(f"Synced {len(self.guilds)} guilds with the database.")

        await self.push_status("ready")

    async def on_resumed(self) -> None:  # type: ignore[override]
        print(">> Gateway session resumed.")
        await self.push_status("ready")

    async def on_disconnect(self) -> None:  # type: ignore[override]
        print(">> Disconnected from gateway.")
        await self.push_status("disconnected")

    async def on_connect(self) -> None:  # type: ignore[override]
        print(">> Connected to gateway.")
        await self.push_status("connecting")

    async def on_guild_join(self, guild: discord.Guild) -> None:  # type: ignore[override]
        await mysql.add_guild(guild.id, guild.name, guild.owner_id)
        dash_url = f"https://modbot.neomechanical.com/dashboard/{guild.id}"

        welcome_message = self.translate(
            "bot.welcome.message",
            placeholders={"dash_url": dash_url},
        )

        view = discord.ui.View()
        view.add_item(
            discord.ui.Button(
                label=self.translate("bot.welcome.button_label"),
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
        await mysql.remove_guild(guild.id)

    async def _load_extensions(self) -> None:
        try:
            for filename in os.listdir("./cogs"):
                path = os.path.join("cogs", filename)
                if not (os.path.isfile(path) and filename.endswith(".py")):
                    continue
                try:
                    await self.load_extension(f"cogs.{filename[:-3]}")
                    if self._log_cog_loads:
                        print(f"Loaded Cog: {filename[:-3]}")
                except Exception as exc:
                    print(f"[FATAL] Failed to load cog {filename}: {exc}")
                    raise
        except Exception:
            raise

    @tasks.loop(hours=6)
    async def cleanup_task(self) -> None:
        await self.wait_until_ready()
        guild_ids = [guild.id for guild in self.guilds]
        print(f"[CLEANUP] Running cleanup for {len(guild_ids)} guilds...")

        await mysql.cleanup_orphaned_guilds(guild_ids)
        await mysql.cleanup_expired_strikes()

    @tasks.loop(seconds=60)
    async def shard_heartbeat(self) -> None:
        await self.wait_until_ready()
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
        await super().close()
