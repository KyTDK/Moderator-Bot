from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord

from modules.core.moderator_bot.i18n import I18nMixin
from modules.i18n.guild_cache import extract_guild_id
from modules.utils import mysql

_logger = logging.getLogger(__name__)


class GuildLocaleMixin(I18nMixin):
    """Mixin handling guild locale caching and resolution."""

    _guild_cache_preload_task: asyncio.Task[None] | None

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
            normalized = self._guild_locales.set_override(guild_id, override)
            if normalized:
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

    def _handle_locale_setting_update(self, guild_id: int, key: str, value: Any) -> None:
        if key != "locale":
            return
        self._guild_locales.set_override(guild_id, value)

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
