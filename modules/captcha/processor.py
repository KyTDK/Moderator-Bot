from __future__ import annotations

import logging
from typing import Iterable

import discord
from discord.ext import commands

from modules.utils import mysql

from .models import (
    CaptchaCallbackPayload,
    CaptchaProcessingError,
    CaptchaWebhookResult,
)

_logger = logging.getLogger(__name__)

class CaptchaCallbackProcessor:
    """Handles captcha callback business logic for a guild member."""

    def __init__(self, bot: commands.Bot):
        self._bot = bot

    async def process(self, payload: CaptchaCallbackPayload) -> CaptchaWebhookResult:
        guild = await self._resolve_guild(payload.guild_id)
        member = await self._resolve_member(guild, payload.user_id)

        settings = await mysql.get_settings(
            guild.id,
            [
                "captcha-verification-enabled",
                "captcha-success-roles",
                "captcha-success-message",
            ],
        )

        if not settings.get("captcha-verification-enabled"):
            _logger.debug(
                "Captcha callback ignored because captcha verification is disabled for guild %s",
                guild.id,
            )
            return CaptchaWebhookResult(status="disabled", roles_applied=0)

        if not payload.success:
            _logger.info(
                "Captcha callback reported failure for user %s in guild %s: %s",
                member.id,
                guild.id,
                payload.failure_reason or "unknown reason",
            )
            return CaptchaWebhookResult(status="failed", roles_applied=0, message=payload.failure_reason)

        role_ids = settings.get("captcha-success-roles") or []
        roles_to_add = _filter_roles(guild, role_ids, member)

        applied = 0
        if roles_to_add:
            try:
                await member.add_roles(*roles_to_add, reason="Captcha verification successful")
                applied = len(roles_to_add)
            except discord.Forbidden as exc:
                raise CaptchaProcessingError(
                    "missing_permissions",
                    "Bot is missing permissions to assign success roles.",
                    http_status=403,
                ) from exc
            except discord.HTTPException as exc:
                raise CaptchaProcessingError(
                    "role_assignment_failed",
                    "Failed to apply success roles due to a Discord API error.",
                ) from exc

        message_template = settings.get("captcha-success-message") or ""
        if message_template:
            try:
                await member.send(message_template)
            except discord.Forbidden:
                _logger.debug("Could not DM captcha success message to user %s", member.id)
            except discord.HTTPException:
                _logger.debug("Failed to send captcha success DM to user %s", member.id)

        return CaptchaWebhookResult(status="ok", roles_applied=applied)

    async def _resolve_guild(self, guild_id: int) -> discord.Guild:
        guild = self._bot.get_guild(guild_id)
        if guild is not None:
            return guild
        try:
            return await self._bot.fetch_guild(guild_id)
        except discord.NotFound as exc:
            raise CaptchaProcessingError(
                "guild_not_found",
                f"Guild {guild_id} is not available to this bot.",
                http_status=404,
            ) from exc
        except discord.HTTPException as exc:
            raise CaptchaProcessingError(
                "guild_fetch_failed",
                f"Failed to fetch guild {guild_id} from Discord.",
            ) from exc

    async def _resolve_member(self, guild: discord.Guild, user_id: int) -> discord.Member:
        member = guild.get_member(user_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(user_id)
        except discord.NotFound as exc:
            raise CaptchaProcessingError(
                "member_not_found",
                f"User {user_id} is not a member of guild {guild.id}.",
                http_status=404,
            ) from exc
        except discord.HTTPException as exc:
            raise CaptchaProcessingError(
                "member_fetch_failed",
                f"Failed to fetch user {user_id} from guild {guild.id}.",
            ) from exc

def _filter_roles(
    guild: discord.Guild,
    role_ids: Iterable[int | str],
    member: discord.Member,
) -> list[discord.Role]:
    roles: list[discord.Role] = []
    for role_id in role_ids:
        try:
            role_int = int(role_id)
        except (TypeError, ValueError):
            _logger.debug("Skipping invalid role id %s for guild %s", role_id, guild.id)
            continue
        role = guild.get_role(role_int)
        if role is None:
            _logger.debug("Guild %s missing configured captcha success role %s", guild.id, role_int)
            continue
        if role in member.roles:
            continue
        roles.append(role)
    return roles
