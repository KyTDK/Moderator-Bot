from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord

from modules.captcha import CaptchaSettingsUpdatePayload
from modules.captcha.client import (
    CaptchaApiClient,
    CaptchaApiError,
    CaptchaGuildConfig,
    CaptchaNotAvailableError,
)
from modules.utils import mysql

from .base import CaptchaBaseMixin

_logger = logging.getLogger(__name__)


class CaptchaEmbedMixin(CaptchaBaseMixin):
    _api_client: CaptchaApiClient
    _config_cache: dict[int, CaptchaGuildConfig]
    _config_cache_expiry: dict[int, float]
    _config_cache_ttl: float

    async def _initial_sync_embeds(self) -> None:
        await self.bot.wait_until_ready()
        for guild in list(self.bot.guilds):
            try:
                await self._sync_guild_embed(guild)
            except Exception:  # pragma: no cover - defensive logging
                _logger.exception(
                    "Failed to synchronise captcha embed for guild %s", guild.id
                )

    async def _handle_stream_setting_update(
        self, payload: CaptchaSettingsUpdatePayload
    ) -> None:
        await self._handle_setting_update(payload.guild_id, payload.key, payload.value)

    async def _handle_setting_update(self, guild_id: int, key: str, value: object) -> None:
        if key not in {
            "captcha-delivery-method",
            "captcha-embed-channel-id",
            "captcha-verification-enabled",
        }:
            return

        try:
            resolved_id = int(guild_id)
        except (TypeError, ValueError):
            return

        guild = self.bot.get_guild(resolved_id)
        if guild is None:
            return

        try:
            await self._sync_guild_embed(guild, force=True)
        except Exception:  # pragma: no cover - defensive logging
            _logger.exception("Failed to update captcha embed for guild %s", resolved_id)

    async def _sync_guild_embed(
        self,
        guild: discord.Guild,
        *,
        force: bool = False,
    ) -> bool:
        settings = await mysql.get_settings(
            guild.id,
            [
                "captcha-verification-enabled",
                "vpn-detection-enabled",
                "captcha-delivery-method",
                "captcha-embed-channel-id",
            ],
        )

        captcha_enabled = bool(settings.get("captcha-verification-enabled"))
        vpn_enabled = bool(settings.get("vpn-detection-enabled"))
        enabled = captcha_enabled or vpn_enabled
        delivery_method = str(settings.get("captcha-delivery-method") or "dm").lower()
        channel_id = self._coerce_positive_int(settings.get("captcha-embed-channel-id"))

        if not enabled or delivery_method != "embed" or not channel_id:
            await self._remove_embed_message(guild)
            return False

        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            await self._remove_embed_message(guild)
            return False

        config = await self._fetch_guild_config(guild.id)
        provider_label = None
        requires_login = True
        if config:
            provider_label = config.provider_label or config.provider
            requires_login = config.delivery.requires_login

        return await self._ensure_embed_message(
            guild,
            channel,
            provider_label,
            requires_login,
            force=force,
        )

    async def _ensure_embed_message(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        provider_label: str | None,
        requires_login: bool,
        *,
        force: bool = False,
    ) -> bool:
        record = await mysql.get_captcha_embed_record(guild.id)
        embed, view = self._build_verification_embed(
            guild,
            provider_label,
            requires_login,
        )

        if record and record.channel_id == channel.id and not force:
            try:
                message = await channel.fetch_message(record.message_id)
            except (discord.NotFound, discord.HTTPException, discord.Forbidden):
                message = None
            if message is not None:
                try:
                    await message.edit(embed=embed, view=view)
                except discord.HTTPException:
                    _logger.warning(
                        "Failed to update captcha embed message for guild %s in channel %s",
                        guild.id,
                        channel.id,
                    )
                    return False
                await mysql.upsert_captcha_embed_record(guild.id, channel.id, message.id)
                return True

        if record:
            await self._remove_embed_message(guild)

        try:
            message = await channel.send(
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            _logger.warning(
                "Missing permissions to send captcha embed in guild %s channel %s",
                guild.id,
                channel.id,
            )
            return False
        except discord.HTTPException:
            _logger.warning(
                "Failed to send captcha embed in guild %s channel %s",
                guild.id,
                channel.id,
            )
            return False

        await mysql.upsert_captcha_embed_record(guild.id, channel.id, message.id)
        return True

    async def _remove_embed_message(self, guild: discord.Guild) -> None:
        record = await mysql.get_captcha_embed_record(guild.id)
        if not record:
            return

        channel = guild.get_channel(record.channel_id)
        if isinstance(channel, discord.TextChannel):
            try:
                message = await channel.fetch_message(record.message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                message = None
            if message is not None:
                try:
                    await message.delete()
                except discord.HTTPException:
                    _logger.info(
                        "Failed to delete existing captcha embed message in guild %s",
                        guild.id,
                    )

        await mysql.delete_captcha_embed_record(guild.id)

    def _build_verification_embed(
        self,
        guild: discord.Guild,
        provider_label: str | None,
        requires_login: bool,
    ) -> tuple[discord.Embed, discord.ui.View]:
        embed_texts: dict[str, Any] = self._translate(
            "cogs.captcha.embed_message",
            guild_id=guild.id,
            fallback={
                "description": (
                    "Click the button below to verify yourself. Once you pass the captcha, "
                    "you will gain access to the rest of the server."
                ),
                "login_notice": "You may be asked to log in to the Moderator Bot dashboard first.",
                "title": "Complete Captcha Verification",
                "footer": "Guild ID: {guild_id} | Powered by Moderator Bot",
                "button_label": "Verify now",
            },
        ) or {}

        description = embed_texts.get(
            "description",
            "Click the button below to verify yourself. Once you pass the captcha, you will gain access to the rest of the server.",
        )
        if requires_login:
            login_notice = embed_texts.get(
                "login_notice",
                "You may be asked to log in to the Moderator Bot dashboard first.",
            )
            if login_notice:
                description = f"{description} {login_notice}" if description else login_notice

        footer_text = embed_texts.get(
            "footer",
            "Guild ID: {guild_id} | Powered by Moderator Bot",
        ).format(guild_id=guild.id)

        embed = self._create_embed(
            title=embed_texts.get("title", "Complete Captcha Verification"),
            description=description,
            footer=footer_text,
            guild_id=guild.id,
        )
        view = self._build_link_view(
            self._build_public_verification_url(guild.id),
            label=embed_texts.get("button_label"),
            guild_id=guild.id,
        )
        return embed, view

    async def _fetch_guild_config(self, guild_id: int) -> CaptchaGuildConfig | None:
        if not self._api_client.is_configured:
            return None

        loop = asyncio.get_running_loop()
        now = loop.time()
        expires_at = self._config_cache_expiry.get(guild_id)
        if expires_at and expires_at > now:
            return self._config_cache.get(guild_id)

        try:
            config = await self._api_client.fetch_guild_config(guild_id)
        except (CaptchaApiError, CaptchaNotAvailableError) as exc:
            _logger.info(
                "Failed to fetch captcha configuration for guild %s: %s",
                guild_id,
                exc,
            )
            self._config_cache.pop(guild_id, None)
            self._config_cache_expiry.pop(guild_id, None)
            return None

        self._config_cache[guild_id] = config
        self._config_cache_expiry[guild_id] = now + self._config_cache_ttl
        return config

