from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Optional

import discord
from discord.utils import utcnow

from modules.captcha.client import (
    CaptchaApiClient,
    CaptchaApiError,
    CaptchaNotAvailableError,
    CaptchaStartResponse,
)
from modules.captcha.sessions import CaptchaSession, CaptchaSessionStore
from modules.utils.time import parse_duration

from .utils import (
    build_dm_description,
    build_embed_delivery_description,
    format_duration,
)

_logger = logging.getLogger(__name__)


class CaptchaDeliveryMixin:
    _api_client: CaptchaApiClient
    _session_store: CaptchaSessionStore

    async def _handle_embed_delivery(
        self,
        member: discord.Member,
        channel_id: int,
        grace_delta: timedelta,
        grace_text: str,
        max_attempts: int | None,
    ) -> bool:
        seeded = await self._seed_embed_requirement(member, grace_delta)
        if not seeded:
            return False

        channel = member.guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            _logger.warning(
                "Captcha embed channel %s not found in guild %s; falling back to DMs",
                channel_id,
                member.guild.id,
            )
            return False

        await self._sync_guild_embed(member.guild)
        await self._notify_member_embed(member, channel, grace_text, max_attempts)
        return True

    async def _seed_embed_requirement(
        self,
        member: discord.Member,
        grace_delta: timedelta,
        *,
        retries: int = 1,
        retry_delay: float = 0.25,
    ) -> bool:
        """Attempt to seed the embed requirement, retrying once for safety."""

        attempts = retries + 1
        for attempt in range(1, attempts + 1):
            try:
                await self._api_client.start_session(member.guild.id, member.id)
            except CaptchaNotAvailableError as exc:
                _logger.info(
                    "Captcha not available for guild %s: %s",
                    member.guild.id,
                    exc,
                )
                return False
            except CaptchaApiError as exc:
                _logger.warning(
                    "Failed to seed captcha requirement for guild %s user %s (attempt %s/%s): %s",
                    member.guild.id,
                    member.id,
                    attempt,
                    attempts,
                    exc,
                )
            except Exception:
                _logger.exception(
                    "Unexpected error when seeding captcha requirement for guild %s user %s",
                    member.guild.id,
                    member.id,
                )
            else:
                session = CaptchaSession(
                    guild_id=member.guild.id,
                    user_id=member.id,
                    token=None,
                    expires_at=utcnow() + grace_delta,
                    delivery_method="embed",
                )
                await self._session_store.put(session)
                return True

            if attempt < attempts:
                await asyncio.sleep(retry_delay)

        return False

    async def _handle_dm_delivery(
        self,
        member: discord.Member,
        grace_setting: str | None,
        max_attempts: int | None,
    ) -> None:
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
            delivery_method="dm",
        )
        await self._session_store.put(session)

        await self._notify_member(
            member,
            start_response,
            grace_period=grace_setting,
            max_attempts=max_attempts,
        )

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

        display_grace = grace_period or format_duration(grace_delta)

        embed = discord.Embed(
            title="Captcha Verification Required",
            description=build_dm_description(member, display_grace, max_attempts),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Powered by Moderator Bot")

        view = discord.ui.View(timeout=float(view_timeout))
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

    async def _notify_member_embed(
        self,
        member: discord.Member,
        channel: discord.TextChannel,
        grace_text: str,
        max_attempts: int | None,
    ) -> None:
        url = self._build_public_verification_url(member.guild.id)
        description = build_embed_delivery_description(
            member,
            channel,
            grace_text,
            max_attempts,
        )

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
