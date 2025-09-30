from __future__ import annotations

import logging
import os
from pathlib import Path
from contextlib import AbstractContextManager
from contextvars import Token
from typing import Any, Optional

import discord
from discord.ext import commands, tasks

from modules.post_stats.topgg_poster import start_topgg_poster
from modules.utils import mysql
from modules.i18n import LocaleRepository, Translator
from modules.i18n.discord_translator import DiscordAppCommandTranslator
from modules.i18n.config import resolve_locales_root
from modules.i18n.guild_cache import GuildLocaleCache, extract_guild_id
from modules.i18n.locale_utils import normalise_locale
from modules.i18n.service import TranslationService
from modules.i18n.resolution import LocaleResolver, LocaleResolution

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
        self._locale_repository: LocaleRepository | None = None
        self._translator: Translator | None = None
        self._translation_service: TranslationService | None = None
        self._command_tree_translator: DiscordAppCommandTranslator | None = None
        self._guild_locales = GuildLocaleCache()
        self._locale_resolver = LocaleResolver(self._guild_locales)
        self._locale_settings_listener = self._handle_locale_setting_update
        self._initialise_i18n()
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

    def _initialise_i18n(self) -> None:
        default_locale = os.getenv("I18N_DEFAULT_LOCALE", "en")
        fallback_locale = os.getenv("I18N_FALLBACK_LOCALE") or default_locale

        configured_root = os.getenv("I18N_LOCALES_DIR")
        legacy_root = os.getenv("LOCALES_DIR")
        if not configured_root and legacy_root:
            _logger.warning(
                "Environment variable LOCALES_DIR is deprecated; please migrate to "
                "I18N_LOCALES_DIR. Using legacy value for now.",
            )
            configured_root = legacy_root

        _logger.warning(
            "Initialising i18n (default=%r, fallback=%r, locales_dir=%r)",
            os.getenv("I18N_DEFAULT_LOCALE"),
            os.getenv("I18N_FALLBACK_LOCALE"),
            configured_root,
        )

        repo_root = Path(__file__).resolve().parents[2]
        locales_root, missing_configured = resolve_locales_root(configured_root, repo_root)

        if configured_root and missing_configured:
            _logger.warning(
                "Configured locales directory %s not found; using %s instead",
                Path(configured_root).expanduser().resolve(),
                locales_root,
            )

        if not locales_root.exists():
            _logger.warning(
                "Locales directory %s does not exist; translations will fall back to keys",
                locales_root,
            )

        _logger.warning(
            "Initialising locale repository (root=%s, default=%s, fallback=%s)",
            locales_root,
            default_locale,
            fallback_locale,
        )

        self._locale_repository = LocaleRepository(
            locales_root,
            default_locale=default_locale,
            fallback_locale=fallback_locale,
        )

        _logger.warning("Ensuring locale repository is loaded")
        self._locale_repository.ensure_loaded()
        try:
            available_locales = self._locale_repository.list_locales()
        except Exception:  # pragma: no cover - defensive logging
            _logger.exception("Failed to list locales after loading repository")
        else:
            _logger.warning(
                "Locale repository ready with %d locales: %s",
                len(available_locales),
                available_locales,
            )

        self._translator = Translator(self._locale_repository)
        _logger.warning(
            "Translator initialised (default=%s, fallback=%s)",
            self._translator.default_locale,
            self._translator.fallback_locale,
        )
        self._translation_service = TranslationService(self._translator)
        _logger.warning("Translation service ready; preparing Discord translator")
        self._command_tree_translator = DiscordAppCommandTranslator(
            self._translation_service
        )

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

    async def refresh_translations(self, *, fetch: bool = True) -> None:
        if self._locale_repository is None:
            _logger.warning("Translation refresh requested but no locale repository is configured")
            return

        if fetch:
            _logger.warning("Crowdin integration has been removed; reloading translations from disk")
        await self._locale_repository.reload_async()
        _logger.warning("Translations reloaded from disk")

    def translate(
        self,
        key: str,
        *,
        guild_id: int | None = None,
        locale: str | None = None,
        placeholders: dict[str, Any] | None = None,
        fallback: str | None = None,
    ) -> Any:
        service = self._translation_service
        if service is None:
            logging.warning(
                "Translation requested but translator has not been initialised"
            )
            return fallback if fallback is not None else key

        resolved_locale = locale
        if resolved_locale is None and guild_id is not None:
            try:
                resolved_locale = self.get_guild_locale(guild_id)
            except ValueError:
                _logger.warning(
                    "Unable to determine guild locale from guild_id=%s; using defaults",
                    guild_id,
                )

        return service.translate(
            key,
            locale=resolved_locale,
            placeholders=placeholders,
            fallback=fallback,
        )

    @property
    def translation_service(self) -> TranslationService:
        if self._translation_service is None:
            raise RuntimeError("Translation service has not been initialised")
        return self._translation_service

    def push_locale(self, locale: Any | None) -> Token[str | None]:
        return self.translation_service.push_locale(locale)

    def reset_locale(self, token: Token[str | None]) -> None:
        self.translation_service.reset_locale(token)

    def use_locale(self, locale: Any | None) -> AbstractContextManager[None]:
        return self.translation_service.use_locale(locale)

    def current_locale(self) -> str | None:
        return self.translation_service.current_locale()

    def infer_locale(self, *candidates: Any) -> LocaleResolution:
        """Infer the locale for the provided *candidates*."""

        return self._locale_resolver.infer(*candidates)

    def resolve_locale(self, *candidates: Any) -> str | None:
        """Convenience wrapper returning the resolved locale string."""

        return self.infer_locale(*candidates).resolved()

    def get_guild_locale(self, guild: Any) -> str | None:
        guild_id = extract_guild_id(guild)
        if guild_id is None:
            try:
                guild_id = int(guild)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                guild_id = None
        if guild_id is None:
            raise ValueError("Unable to determine guild ID from candidate")

        override = self._guild_locales.get_override(guild_id)
        if override:
            return override

        return self._guild_locales.get(guild_id)

    async def _preload_guild_locale_cache(self) -> None:
        """Warm the in-memory guild locale cache from the database."""

        try:
            stored_locales = await mysql.get_all_guild_locales()
        except Exception:
            _logger.exception("Failed to preload guild locales from database")
            return

        if not stored_locales:
            return

        self._guild_locales.preload(stored_locales)

    async def _resolve_guild_owner_id(self, guild: discord.Guild) -> int | None:
        owner_id = getattr(guild, "owner_id", None)
        if owner_id is not None:
            return int(owner_id)

        owner = getattr(guild, "owner", None)
        if owner is not None:
            return owner.id

        try:
            fetched = await self.fetch_guild(guild.id)
        except discord.HTTPException as exc:
            _logger.warning(
                "Unable to resolve owner for guild %s (%s): %s",
                guild.id,
                guild.name,
                exc,
            )
            return None

        fetched_owner_id = getattr(fetched, "owner_id", None)
        if fetched_owner_id is None:
            _logger.warning(
                "Fetched guild %s (%s) but no owner ID was provided", guild.id, guild.name
            )
            return None

        return int(fetched_owner_id)

    async def refresh_guild_locale_override(self, guild_id: int) -> None:
        override: Any | None = None
        fallback: Any | None = None

        try:
            override = await mysql.get_settings(guild_id, "locale")
        except Exception:
            _logger.exception("Failed to load locale override for guild %s", guild_id)

        if override is not None:
            _logger.warning(
                "Loaded locale override for guild %s: %r", guild_id, override
            )
            normalized = self._guild_locales.set_override(guild_id, override)
            if normalized:
                _logger.debug(
                    "Using saved override for guild %s -> %s", guild_id, normalized
                )
                return

            _logger.warning(
                "Stored override for guild %s (%r) is not a supported locale; "
                "falling back to guild preference",
                guild_id,
                override,
            )

        try:
            fallback = await mysql.get_guild_locale(guild_id)
        except Exception:
            _logger.exception(
                "Failed to load guild locale fallback for guild %s", guild_id
            )
            fallback = None

        self._guild_locales.set_override(guild_id, None)
        self._guild_locales.store(guild_id, fallback)

    def _handle_locale_setting_update(
        self, guild_id: int, key: str, value: Any
    ) -> None:
        if key != "locale":
            return
        self._guild_locales.set_override(guild_id, value)

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
        if self._command_tree_translator is not None:
            _logger.warning("Setting Discord command tree translator")
            await self.tree.set_translator(self._command_tree_translator)

        try:
            await mysql.initialise_and_get_pool()
        except Exception as exc:
            print(f"[FATAL] MySQL init failed: {exc}")
            raise

        await self._preload_guild_locale_cache()

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
                preferred = getattr(guild, "preferred_locale", None)
                normalized_locale = normalise_locale(preferred)
                owner_id = await self._resolve_guild_owner_id(guild)
                if owner_id is None:
                    _logger.warning(
                        "Skipping guild sync for %s (%s); owner ID unavailable",
                        guild.id,
                        guild.name,
                    )
                    continue
                await mysql.add_guild(guild.id, guild.name, owner_id, normalized_locale)
                await self.refresh_guild_locale_override(guild.id)
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
        await mysql.add_guild(guild.id, guild.name, owner_id, normalized_locale)
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
        await mysql.remove_guild(guild.id)
        self._guild_locales.drop(guild.id)

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
        mysql.remove_settings_listener(self._locale_settings_listener)
        await super().close()
