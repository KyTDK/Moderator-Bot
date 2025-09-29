from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Optional

import discord
from discord.ext import commands, tasks

from modules.post_stats.topgg_poster import start_topgg_poster
from modules.utils import mysql
from modules.i18n import LocaleRepository, Translator
from modules.i18n.locale_utils import SUPPORTED_LOCALE_ALIASES, normalise_locale

_current_locale: ContextVar[str | None] = ContextVar("moderator_bot_locale", default=None)

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
        self._guild_locale_overrides: dict[int, str | None] = {}
        self._guild_locales: dict[int, str | None] = {}
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
        locales_override = os.getenv("I18N_LOCALES_DIR")

        base_root = locales_override or os.getenv("LOCALES_DIR") or "locales"
        locales_root = Path(base_root).resolve()

        _logger.info(
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

    async def refresh_translations(self, *, fetch: bool = True) -> None:
        if self._locale_repository is None:
            _logger.warning("Translation refresh requested but no locale repository is configured")
            return

        if fetch:
            _logger.info("Crowdin integration has been removed; reloading translations from disk")
        await self._locale_repository.reload_async()
        _logger.info("Translations reloaded from disk")

    def dispatch(self, event_name: str, /, *args: Any, **kwargs: Any) -> None:
        locale = self._infer_locale_from_event(event_name, args, kwargs)
        token = _current_locale.set(locale)
        try:
            super().dispatch(event_name, *args, **kwargs)
        finally:
            _current_locale.reset(token)

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
        if locale is None:
            locale = _current_locale.get()
        else:
            locale = self._normalise_locale(locale)
        return translator.translate(
            key,
            locale=locale,
            placeholders=placeholders,
            fallback=fallback,
        )

    @contextmanager
    def locale_context(self, locale: str | None):
        normalized = self._normalise_locale(locale)
        token = _current_locale.set(normalized)
        try:
            yield
        finally:
            _current_locale.reset(token)

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        locale = self._extract_locale_from_interaction(interaction)
        token = _current_locale.set(locale)
        try:
            try:
                parent_on_interaction = super().on_interaction  # type: ignore[attr-defined]
            except AttributeError:
                parent_on_interaction = None

            if parent_on_interaction is not None:
                await parent_on_interaction(interaction)
        finally:
            _current_locale.reset(token)

    async def invoke(self, ctx: commands.Context[Any]) -> None:  # type: ignore[override]
        locale = self._extract_locale_from_context(ctx)
        token = _current_locale.set(locale)
        try:
            await super().invoke(ctx)
        finally:
            _current_locale.reset(token)

    def resolve_locale_for_interaction(
        self, interaction: discord.Interaction
    ) -> str | None:
        """Return the locale resolved for *interaction* using translation logic."""

        return self._extract_locale_from_interaction(interaction)

    def _extract_locale_from_interaction(
        self, interaction: discord.Interaction
    ) -> str | None:
        guild_override = self._get_guild_locale_override_from_candidate(interaction)
        if guild_override:
            _logger.debug(
                "Resolved locale for interaction via override: %s", guild_override
            )
            return guild_override

        stored_locale = self._get_stored_guild_locale_from_candidate(interaction)
        if stored_locale:
            _logger.debug("Resolved locale for interaction via stored cache: %s", stored_locale)
            return stored_locale

        guild = getattr(interaction, "guild", None)
        if guild is not None:
            preferred = getattr(guild, "preferred_locale", None)
            stored = self._store_guild_locale(guild.id, preferred)
            if stored:
                _logger.debug(
                    "Resolved locale for interaction via guild preferred locale %r -> %s",
                    preferred,
                    stored,
                )
                return stored

        direct_locale = getattr(interaction, "locale", None)
        normalized = self._normalise_locale(direct_locale)
        _logger.debug(
            "Resolved locale for interaction via direct locale %r -> %s",
            direct_locale,
            normalized,
        )
        return normalized

    def _extract_locale_from_context(
        self, ctx: commands.Context[Any]
    ) -> str | None:
        guild_override = self._get_guild_locale_override_from_candidate(ctx)
        if guild_override:
            _logger.debug("Resolved locale for context via override: %s", guild_override)
            return guild_override

        stored_locale = self._get_stored_guild_locale_from_candidate(ctx)
        if stored_locale:
            _logger.debug("Resolved locale for context via stored cache: %s", stored_locale)
            return stored_locale

        guild = getattr(ctx, "guild", None)
        if guild is None:
            return None

        preferred = getattr(guild, "preferred_locale", None)
        resolved = self._store_guild_locale(guild.id, preferred)
        _logger.debug(
            "Resolved locale for context via guild preferred locale %r -> %s",
            preferred,
            resolved,
        )
        return resolved

    def _infer_locale_from_event(
        self, _event_name: str, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> str | None:
        for candidate in (*args, *kwargs.values()):
            locale = self._extract_locale_from_event_object(candidate)
            if locale:
                return locale

        return None

    def _extract_locale_from_event_object(self, candidate: Any) -> str | None:
        if candidate is None:
            return None

        override = self._get_guild_locale_override_from_candidate(candidate)
        if override:
            _logger.debug("Resolved locale for event object via override: %s", override)
            return override

        stored_locale = self._get_stored_guild_locale_from_candidate(candidate)
        if stored_locale:
            _logger.debug(
                "Resolved locale for event object via stored cache: %s", stored_locale
            )
            return stored_locale

        guild = getattr(candidate, "guild", None)
        if guild is not None:
            preferred = getattr(guild, "preferred_locale", None)
            normalized = self._store_guild_locale(guild.id, preferred)
            if normalized:
                _logger.debug(
                    "Resolved locale for event object via guild preferred locale %r -> %s",
                    preferred,
                    normalized,
                )
                return normalized

        direct_locale = getattr(candidate, "locale", None)
        normalized = self._normalise_locale(direct_locale)
        _logger.debug(
            "Resolved locale for event object via direct locale %r -> %s",
            direct_locale,
            normalized,
        )
        return normalized

    @staticmethod
    def _extract_guild_id(candidate: Any) -> int | None:
        guild = getattr(candidate, "guild", None)
        if guild is not None:
            guild_id = getattr(guild, "id", None)
            if guild_id is not None:
                try:
                    return int(guild_id)
                except (TypeError, ValueError):
                    return None
        guild_id = getattr(candidate, "guild_id", None)
        if guild_id is not None:
            try:
                return int(guild_id)
            except (TypeError, ValueError):
                return None
        return None

    def _get_guild_locale_override_from_candidate(self, candidate: Any) -> str | None:
        guild_id = self._extract_guild_id(candidate)
        if guild_id is None:
            return None
        override = self._guild_locale_overrides.get(guild_id)
        if override:
            _logger.debug("Loaded guild locale override (guild_id=%s): %s", guild_id, override)
            return override
        return None

    def _normalise_locale(self, locale: Any) -> str | None:
        normalized = normalise_locale(locale)
        if locale and normalized is None:
            _logger.debug("Failed to normalise locale %r", locale)
        elif locale:
            _logger.debug("Normalised locale %r -> %s", locale, normalized)
        return normalized

    def _store_guild_locale(self, guild_id: int, locale: Any) -> str | None:
        normalized = self._normalise_locale(locale)
        self._guild_locales[guild_id] = normalized
        _logger.debug(
            "Stored guild locale (guild_id=%s): input=%r normalized=%s",
            guild_id,
            locale,
            normalized,
        )
        return normalized

    def _get_stored_guild_locale(self, guild_id: int) -> str | None:
        return self._guild_locales.get(guild_id)

    def _get_stored_guild_locale_from_candidate(self, candidate: Any) -> str | None:
        guild_id = self._extract_guild_id(candidate)
        if guild_id is None:
            return None
        return self._get_stored_guild_locale(guild_id)

    async def refresh_guild_locale_override(self, guild_id: int) -> None:
        try:
            override = await mysql.get_settings(guild_id, "locale")
        except Exception:
            _logger.exception("Failed to load locale override for guild %s", guild_id)
            return
        normalized = self._normalise_locale(override)
        self._guild_locale_overrides[guild_id] = normalized
        _logger.debug(
            "Refreshed guild locale override (guild_id=%s): %r -> %s",
            guild_id,
            override,
            normalized,
        )

    def _handle_locale_setting_update(
        self, guild_id: int, key: str, value: Any
    ) -> None:
        if key != "locale":
            return
        normalized = self._normalise_locale(value)
        self._guild_locale_overrides[guild_id] = normalized
        _logger.debug(
            "Updated guild locale override via settings (guild_id=%s): %r -> %s",
            guild_id,
            value,
            normalized,
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
                preferred = getattr(guild, "preferred_locale", None)
                normalized_locale = self._store_guild_locale(guild.id, preferred)
                await mysql.add_guild(
                    guild.id, guild.name, guild.owner_id, normalized_locale
                )
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
        normalized_locale = self._store_guild_locale(guild.id, preferred)
        await mysql.add_guild(guild.id, guild.name, guild.owner_id, normalized_locale)
        await self.refresh_guild_locale_override(guild.id)
        dash_url = f"https://modbot.neomechanical.com/dashboard/{guild.id}"

        override = self._guild_locale_overrides.get(guild.id)
        if override and override != _current_locale.get():
            with self.locale_context(override):
                welcome_message = self.translate(
                    "bot.welcome.message",
                    placeholders={"dash_url": dash_url},
                )
                button_label = self.translate("bot.welcome.button_label")
        else:
            welcome_message = self.translate(
                "bot.welcome.message",
                placeholders={"dash_url": dash_url},
            )
            button_label = self.translate("bot.welcome.button_label")

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
        self._guild_locales.pop(guild.id, None)

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
