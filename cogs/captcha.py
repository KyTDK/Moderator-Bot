from __future__ import annotations

import logging
import os
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import discord
from discord.ext import commands

from modules.utils import mysql

from modules.captcha import CaptchaWebhookConfig, CaptchaWebhookServer
from modules.captcha.client import (
    CaptchaApiClient,
    CaptchaApiError,
    CaptchaNotAvailableError,
    CaptchaStartResponse,
)
from modules.captcha.sessions import CaptchaSession, CaptchaSessionStore

_DEFAULT_API_BASE = "https://modbot.neomechanical.com/api/captcha"
_logger = logging.getLogger(__name__)

class CaptchaCog(commands.Cog):
    """Captcha verification flow for new guild members."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._session_store = CaptchaSessionStore()
        self._api_base = _resolve_api_base()
        self._api_client = CaptchaApiClient(self._api_base, os.getenv("CAPTCHA_API_TOKEN"))
        self._webhook_config = CaptchaWebhookConfig.from_env()
        self._webhook = CaptchaWebhookServer(bot, self._webhook_config, self._session_store)

        if not self._api_client.is_configured:
            _logger.warning(
                "CAPTCHA_API_TOKEN or API base URL missing; captcha verification will be disabled."
            )

    async def cog_load(self) -> None:
        await self._webhook.start()

    async def cog_unload(self) -> None:
        await self._webhook.stop()
        await self._api_client.close()

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot or member.guild is None:
            return

        enabled = await mysql.get_settings(
            member.guild.id, "captcha-verification-enabled",)
        if not enabled:
            return

        if not self._api_client.is_configured:
            _logger.debug(
                "Captcha API not configured; skipping verification for guild %s", member.guild.id
            )
            return

        try:
            start_response = await self._api_client.start_session(member.guild.id, member.id)
        except CaptchaNotAvailableError as exc:
            _logger.info(
                "Captcha not available for guild %s: %s",
                member.guild.id,
                exc,
            )
            return
        except CaptchaApiError as exc:
            _logger.warning(
                "Failed to start captcha session for guild %s user %s: %s",
                member.guild.id,
                member.id,
                exc,
            )
            return
        except Exception:
            _logger.exception(
                "Unexpected error when creating captcha session for guild %s user %s",
                member.guild.id,
                member.id,
            )
            return

        session = CaptchaSession(
            guild_id=start_response.guild_id,
            user_id=start_response.user_id,
            token=start_response.token,
            expires_at=start_response.expires_at,
            state=start_response.state,
        )
        await self._session_store.put(session)

        await self._notify_member(member, start_response)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if member.guild is None:
            return
        await self._session_store.remove(member.guild.id, member.id)

    async def _notify_member(self, member: discord.Member, response: CaptchaStartResponse) -> None:
        # Build an embed
        embed = discord.Embed(
            title="Captcha Verification Required",
            description=(
                f"Hi {member.mention}! To finish joining **{member.guild.name}**, "
                "please complete the captcha within **10 minutes**."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Powered by Moderator Bot")

        # Build a button view (link style)
        view = discord.ui.View(timeout=600)
        view.add_item(
            discord.ui.Button(
                label="Click here to verify",
                url=response.verification_url,
                style=discord.ButtonStyle.link,
            )
        )

        # Try DM first
        try:
            await member.send(embed=embed, view=view)
            return
        except discord.Forbidden:
            _logger.debug("Could not DM captcha instructions to user %s", member.id)
        except discord.HTTPException:
            _logger.debug("Failed to DM captcha instructions to user %s", member.id)

        # Fallback to a guild channel
        channel = self._find_fallback_channel(member.guild)
        if channel is None:
            return

        try:
            await channel.send(
                content=member.mention,
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except discord.HTTPException:
            _logger.debug(
                "Failed to post captcha instructions in guild %s for user %s",
                member.guild.id,
                member.id,
            )

    def _find_fallback_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        me = guild.me
        if me is None and self.bot.user is not None:
            me = guild.get_member(self.bot.user.id)

        if me is None:
            return None

        if guild.system_channel and guild.system_channel.permissions_for(me).send_messages:
            return guild.system_channel

        for channel in guild.text_channels:
            if channel.permissions_for(me).send_messages:
                return channel

        return None

def _resolve_api_base() -> str:
    raw = os.getenv("CAPTCHA_PUBLIC_VERIFY_URL")
    if not raw:
        return _DEFAULT_API_BASE

    base = raw.strip()
    if not base:
        return _DEFAULT_API_BASE

    parts = urlsplit(base)
    path = parts.path

    if path.endswith("/start"):
        path = path[: -len("/start")]

    path = path.rstrip("/")

    if path.endswith("/accelerated/captcha"):
        path = f"{path[: -len('/accelerated/captcha')]}/api/captcha"
    elif path.endswith("/captcha"):
        path = f"{path[: -len('/captcha')]}/api/captcha"
    elif not path.endswith("/api/captcha"):
        if path:
            path = f"{path}/api/captcha"
        else:
            path = "/api/captcha"

    rebuilt = parts._replace(path=path, query="", fragment="")
    return urlunsplit(rebuilt).rstrip("/") or _DEFAULT_API_BASE

async def setup(bot: commands.Bot) -> None:
    cog = CaptchaCog(bot)
    await bot.add_cog(cog)
