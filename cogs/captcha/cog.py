from __future__ import annotations

import asyncio
import logging
import os
from datetime import timedelta

import discord
from discord import Interaction, app_commands
from discord.ext import commands

from modules.captcha import CaptchaStreamConfig, CaptchaStreamListener
from modules.captcha.client import CaptchaApiClient, CaptchaGuildConfig
from modules.captcha.sessions import CaptchaSessionStore
from modules.utils import mysql
from modules.utils.discord_utils import resolve_role_references
from modules.utils.time import parse_duration

from .config import resolve_api_base, resolve_public_verify_url
from .delivery import CaptchaDeliveryMixin
from .embed import CaptchaEmbedMixin

_logger = logging.getLogger(__name__)


class CaptchaCog(CaptchaEmbedMixin, CaptchaDeliveryMixin, commands.Cog):
    """Captcha verification flow for new guild members."""

    captcha_group = app_commands.Group(
        name="captcha",
        description="Manage captcha verification.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot
        self._session_store = CaptchaSessionStore()
        self._api_base = resolve_api_base()
        self._api_client = CaptchaApiClient(
            self._api_base, os.getenv("CAPTCHA_API_TOKEN")
        )
        self._stream_config = CaptchaStreamConfig.from_env()
        self._stream_listener = CaptchaStreamListener(
            bot,
            self._stream_config,
            self._session_store,
            self._handle_stream_setting_update,
        )
        self._public_verify_url = resolve_public_verify_url()
        self._settings_listener_registered = False
        self._embed_sync_task: asyncio.Task[None] | None = None
        self._config_cache: dict[int, CaptchaGuildConfig] = {}
        self._config_cache_expiry: dict[int, float] = {}
        self._config_cache_ttl = 300.0

        if not self._api_client.is_configured:
            _logger.warning(
                "CAPTCHA_API_TOKEN or API base URL missing; captcha verification will be disabled."
            )

    @captcha_group.command(name="sync", description="Resend the captcha verification embed.")
    async def sync_embed_command(self, interaction: Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            updated = await self._sync_guild_embed(interaction.guild, force=True)
        except Exception:
            _logger.exception(
                "Failed to synchronise captcha embed for guild %s via command", interaction.guild.id
            )
            await interaction.followup.send(
                "An unexpected error occurred while updating the captcha embed.",
                ephemeral=True,
            )
            return

        if updated:
            await interaction.followup.send(
                "Captcha verification embed has been updated.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "Captcha embed delivery is not configured for this server.",
                ephemeral=True,
            )

    async def cog_load(self) -> None:
        started = await self._stream_listener.start()
        if started:
            print(
                "[CAPTCHA] Redis stream listener subscribed to "
                f"{self._stream_config.stream} as "
                f"{self._stream_config.group}/{self._stream_config.consumer_name}"
            )
        else:
            print(
                "[CAPTCHA] Captcha Redis stream listener disabled; callbacks will not be processed."
            )

        if not self._settings_listener_registered:
            mysql.add_settings_listener(self._handle_setting_update)
            self._settings_listener_registered = True

        if self._embed_sync_task is None:
            self._embed_sync_task = asyncio.create_task(self._initial_sync_embeds())

    async def cog_unload(self) -> None:
        await self._stream_listener.stop()
        await self._api_client.close()
        if self._settings_listener_registered:
            mysql.remove_settings_listener(self._handle_setting_update)
            self._settings_listener_registered = False
        if self._embed_sync_task is not None:
            self._embed_sync_task.cancel()
            try:
                await self._embed_sync_task
            except asyncio.CancelledError:
                pass
            self._embed_sync_task = None

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot or member.guild is None:
            return

        settings = await mysql.get_settings(
            member.guild.id,
            [
                "captcha-verification-enabled",
                "captcha-grace-period",
                "captcha-max-attempts",
                "pre-captcha-roles",
                "captcha-delivery-method",
                "captcha-embed-channel-id",
            ],
        )

        if not settings.get("captcha-verification-enabled"):
            return

        if not self._api_client.is_configured:
            _logger.debug(
                "Captcha API not configured; skipping verification for guild %s", member.guild.id
            )
            return

        raw_pre_roles = settings.get("pre-captcha-roles") or []
        pre_roles = [
            role
            for role in resolve_role_references(
                member.guild,
                raw_pre_roles,
                allow_names=False,
                logger=_logger,
            )
            if role not in member.roles
        ]
        if pre_roles:
            try:
                await member.add_roles(*pre_roles, reason="Assigning pre-captcha roles")
            except discord.Forbidden:
                _logger.warning(
                    "Missing permissions to assign pre-captcha roles in guild %s",
                    member.guild.id,
                )
            except discord.HTTPException:
                _logger.warning(
                    "Failed to assign pre-captcha roles in guild %s for user %s",
                    member.guild.id,
                    member.id,
                )

        delivery_method = str(settings.get("captcha-delivery-method") or "dm").lower()
        embed_channel_id = self._coerce_positive_int(settings.get("captcha-embed-channel-id"))
        grace_setting = self._coerce_grace_period(settings.get("captcha-grace-period"))
        grace_delta = parse_duration(grace_setting) if grace_setting else None
        if grace_delta is None:
            grace_delta = timedelta(minutes=10)
        grace_display = grace_setting or self._format_duration(grace_delta)
        max_attempts = self._coerce_positive_int(settings.get("captcha-max-attempts"))

        if delivery_method == "embed" and embed_channel_id:
            handled = await self._handle_embed_delivery(
                member,
                embed_channel_id,
                grace_delta,
                grace_display,
                max_attempts,
            )
            if handled:
                return

        await self._handle_dm_delivery(
            member,
            grace_setting,
            max_attempts,
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if member.guild is None:
            return
        await self._session_store.remove(member.guild.id, member.id)

async def setup_captcha(bot: commands.Bot) -> None:
    await bot.add_cog(CaptchaCog(bot))