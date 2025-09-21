from __future__ import annotations

import logging
import os
from datetime import timedelta
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import discord
from discord.ext import commands
from discord.utils import utcnow

from modules.utils import mysql
from modules.utils.discord_utils import resolve_role_references
from modules.utils.time import parse_duration

from modules.captcha import CaptchaStreamConfig, CaptchaStreamListener
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
        self._stream_config = CaptchaStreamConfig.from_env()
        self._stream_listener = CaptchaStreamListener(bot, self._stream_config, self._session_store)

        if not self._api_client.is_configured:
            _logger.warning(
                "CAPTCHA_API_TOKEN or API base URL missing; captcha verification will be disabled."
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
            print("[CAPTCHA] Captcha Redis stream listener disabled; callbacks will not be processed.")

    async def cog_unload(self) -> None:
        await self._stream_listener.stop()
        await self._api_client.close()

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
            ],
        )

        if not settings.get("captcha-verification-enabled"):
            return

        if not self._api_client.is_configured:
            _logger.debug(
                "Captcha API not configured; skipping verification for guild %s", member.guild.id
            )
            return

        # Give pre-captcha roles if configured
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
        
        try:
            start_response = await self._api_client.start_session(
                member.guild.id,
                member.id,
            )
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

        await self._notify_member(
            member,
            start_response,
            grace_period=self._coerce_grace_period(settings.get("captcha-grace-period")),
            max_attempts=self._coerce_positive_int(settings.get("captcha-max-attempts")),
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if member.guild is None:
            return
        await self._session_store.remove(member.guild.id, member.id)

    async def _notify_member(
        self,
        member: discord.Member,
        response: CaptchaStartResponse,
        *,
        grace_period: str | None,
        max_attempts: int | None,
    ) -> None:
        grace_delta = parse_duration(grace_period) if grace_period else None
        if grace_delta is None:
            grace_delta = timedelta(minutes=10)
        grace_seconds = int(grace_delta.total_seconds()) if grace_delta else 600
        if grace_seconds <= 0:
            grace_seconds = 600

        expires_in: int | None = None
        if response.expires_at:
            remaining = int((response.expires_at - utcnow()).total_seconds())
            expires_in = remaining if remaining > 0 else None

        view_timeout = grace_seconds
        if expires_in is not None:
            view_timeout = max(1, min(grace_seconds, expires_in))

        display_grace = grace_period or self._format_duration(grace_delta)

        # Build an embed
        embed = discord.Embed(
            title="Captcha Verification Required",
            description=self._build_description(member, display_grace, max_attempts),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Powered by Moderator Bot")

        # Build a button view (link style)
        view = discord.ui.View(timeout=float(view_timeout))
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

    @staticmethod
    def _coerce_positive_int(value: object) -> int | None:
        try:
            number = int(str(value))
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    @staticmethod
    def _coerce_grace_period(value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _format_duration(delta: timedelta | None) -> str:
        if not delta:
            return "a few minutes"
        total_seconds = int(delta.total_seconds())
        if total_seconds < 60:
            return f"{total_seconds} second{'s' if total_seconds != 1 else ''}"
        minutes, seconds = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        days, hours = divmod(hours, 24)

        parts: list[str] = []
        if days:
            parts.append(f"{days} day{'s' if days != 1 else ''}")
        if hours:
            parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
        if minutes:
            parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
        if not parts and seconds:
            parts.append(f"{seconds} second{'s' if seconds != 1 else ''}")
        return ", ".join(parts)

    def _build_description(
        self,
        member: discord.Member,
        grace_text: str,
        max_attempts: int | None,
    ) -> str:
        description = (
            f"Hi {member.mention}! To finish joining **{member.guild.name}**, "
            f"please complete the captcha within **{grace_text}**."
        )
        if max_attempts:
            attempt_label = "attempt" if max_attempts == 1 else "attempts"
            description += f" You have **{max_attempts}** {attempt_label}."
        return description

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
