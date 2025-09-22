from __future__ import annotations

import logging
from datetime import timedelta

import discord
from discord.utils import utcnow

from modules.captcha.client import (
    CaptchaApiClient,
    CaptchaApiError,
    CaptchaNotAvailableError,
    CaptchaStartResponse,
)
from modules.captcha.sessions import CaptchaSession, CaptchaSessionStore
from .base import CaptchaBaseMixin

_logger = logging.getLogger(__name__)


class CaptchaDeliveryMixin(CaptchaBaseMixin):
    _api_client: CaptchaApiClient
    _session_store: CaptchaSessionStore

    async def _handle_embed_delivery(
        self,
        member: discord.Member,
        channel_id: int,
        grace_delta: timedelta | None,
        grace_text: str | None,
        max_attempts: int | None,
    ) -> CaptchaSession | None:
        try:
            await self._api_client.start_session(member.guild.id, member.id)
        except (CaptchaApiError, CaptchaNotAvailableError) as exc:
            _logger.warning(
                "Failed to seed captcha requirement for guild %s user %s: %s",
                member.guild.id,
                member.id,
                exc,
            )

        expires_at = utcnow() + grace_delta if grace_delta is not None else None

        session = CaptchaSession(
            guild_id=member.guild.id,
            user_id=member.id,
            token=None,
            expires_at=expires_at,
            delivery_method="embed",
        )
        await self._session_store.put(session)

        channel = member.guild.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            await self._sync_guild_embed(member.guild)
            await self._notify_member_embed(
                member,
                channel,
                grace_text,
                max_attempts,
            )
            return session

        _logger.warning(
            "Captcha embed channel %s not found in guild %s; falling back to DMs",
            channel_id,
            member.guild.id,
        )
        return None

    async def _handle_dm_delivery(
        self,
        member: discord.Member,
        max_attempts: int | None,
        grace_delta: timedelta | None,
        grace_text: str | None,
    ) -> CaptchaStartResponse | None:
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
            return None
        except Exception:
            _logger.exception(
                "Unexpected error when creating captcha session for guild %s user %s",
                member.guild.id,
                member.id,
            )
            return None

        session = CaptchaSession(
            guild_id=start_response.guild_id,
            user_id=start_response.user_id,
            token=start_response.token,
            expires_at=start_response.expires_at if grace_delta is not None else None,
            state=start_response.state,
            delivery_method="dm",
        )
        await self._session_store.put(session)

        await self._notify_member(
            member,
            start_response,
            grace_delta=grace_delta,
            grace_text=grace_text,
            max_attempts=max_attempts,
        )
        return start_response

    async def _notify_member(
        self,
        member: discord.Member,
        response: CaptchaStartResponse,
        *,
        grace_delta: timedelta | None,
        grace_text: str | None,
        max_attempts: int | None,
    ) -> None:
        expires_in: int | None = None
        if grace_delta is not None and response.expires_at:
            remaining = int((response.expires_at - utcnow()).total_seconds())
            expires_in = remaining if remaining > 0 else None

        view_timeout: int | None
        if grace_delta is None:
            view_timeout = None
        else:
            grace_seconds = max(1, int(grace_delta.total_seconds()))
            view_timeout = grace_seconds
            if expires_in is not None:
                view_timeout = max(1, min(grace_seconds, expires_in))

        display_grace = grace_text or (
            self._format_duration(grace_delta) if grace_delta is not None else None
        )

        embed = discord.Embed(
            title="Captcha Verification Required",
            description=self._build_description(member, display_grace, max_attempts),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Powered by Moderator Bot")

        timeout_value = None if view_timeout is None else float(view_timeout)
        view = discord.ui.View(timeout=timeout_value)
        view.add_item(
            discord.ui.Button(
                label="Click here to verify",
                url=response.verification_url,
                style=discord.ButtonStyle.link,
            )
        )

        try:
            await member.send(embed=embed, view=view)
            return
        except discord.Forbidden:
            _logger.debug("Could not DM captcha instructions to user %s", member.id)
        except discord.HTTPException:
            _logger.debug("Failed to DM captcha instructions to user %s", member.id)

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

    async def _notify_member_embed(
        self,
        member: discord.Member,
        channel: discord.TextChannel,
        grace_text: str | None,
        max_attempts: int | None,
    ) -> None:
        url = self._build_public_verification_url(member.guild.id)
        if grace_text:
            description = (
                f"Hi {member.mention}! To finish joining **{member.guild.name}**, please visit "
                f"{channel.mention} and complete the captcha within **{grace_text}**."
            )
        else:
            description = (
                f"Hi {member.mention}! To finish joining **{member.guild.name}**, please visit "
                f"{channel.mention} and complete the captcha when you're ready."
            )
        if max_attempts:
            attempt_label = "attempt" if max_attempts == 1 else "attempts"
            description += f" You have **{max_attempts}** {attempt_label}."

        embed = discord.Embed(
            title="Captcha Verification Required",
            description=description,
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Need help?",
            value="Click the button below to open the verification page.",
            inline=False,
        )
        embed.set_footer(text="Powered by Moderator Bot")

        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                label="Open verification page",
                url=url,
                style=discord.ButtonStyle.link,
            )
        )

        try:
            await member.send(embed=embed, view=view)
        except (discord.Forbidden, discord.HTTPException):
            _logger.debug("Failed to DM captcha embed instructions to user %s", member.id)
