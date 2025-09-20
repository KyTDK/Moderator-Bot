﻿from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import discord
from discord.ext import commands

from modules.utils import mysql
from modules.utils import mod_logging
from modules.utils.time import parse_duration
from modules.moderation import strike

from .models import (
    CaptchaCallbackPayload,
    CaptchaProcessingError,
    CaptchaWebhookResult,
)
from .sessions import CaptchaSessionStore

_logger = logging.getLogger(__name__)

class CaptchaCallbackProcessor:
    """Handles captcha callback business logic for a guild member."""

    def __init__(self, bot: commands.Bot, session_store: CaptchaSessionStore):
        self._bot = bot
        self._sessions = session_store

    async def process(self, payload: CaptchaCallbackPayload) -> CaptchaWebhookResult:
        session = await self._sessions.get(payload.guild_id, payload.user_id)
        if session is None:
            raise CaptchaProcessingError(
                "unknown_token",
                "No pending captcha verification found for this user.",
                http_status=404,
            )

        if session.token != payload.token:
            raise CaptchaProcessingError(
                "token_mismatch",
                "Captcha token does not match the pending verification.",
                http_status=404,
            )

        guild = await self._resolve_guild(payload.guild_id)
        member = await self._resolve_member(guild, payload.user_id)

        settings = await mysql.get_settings(
            guild.id,
            [
                "captcha-verification-enabled",
                "captcha-success-roles",
                "captcha-success-message",
                "captcha-failure-actions",
                "captcha-max-attempts",
            ],
        )

        if not settings.get("captcha-verification-enabled"):
            _logger.debug(
                "Captcha callback ignored because captcha verification is disabled for guild %s",
                guild.id,
            )
            await self._sessions.remove(payload.guild_id, payload.user_id)
            return CaptchaWebhookResult(status="disabled", roles_applied=0)

        if not payload.success:
            _logger.info(
                "Captcha callback reported failure for user %s in guild %s: %s",
                member.id,
                guild.id,
                payload.failure_reason or "unknown reason",
            )
            await self._apply_failure_actions(member, payload, settings)
            await self._sessions.remove(payload.guild_id, payload.user_id)
            return CaptchaWebhookResult(
                status="failed",
                roles_applied=0,
                message=payload.failure_reason,
            )

        role_ids = settings.get("captcha-success-roles") or []
        roles_to_add = _filter_roles(guild, role_ids, member)

        applied = 0
        session_consumed = False
        if roles_to_add:
            try:
                await member.add_roles(*roles_to_add, reason="Captcha verification successful")
                applied = len(roles_to_add)
                session_consumed = True
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
        else:
            session_consumed = True

        message_template = settings.get("captcha-success-message") or ""
        if message_template:
            try:
                await member.send(message_template)
            except discord.Forbidden:
                _logger.debug("Could not DM captcha success message to user %s", member.id)
            except discord.HTTPException:
                _logger.debug("Failed to send captcha success DM to user %s", member.id)

        if session_consumed:
            await self._sessions.remove(payload.guild_id, payload.user_id)

        return CaptchaWebhookResult(status="ok", roles_applied=applied)

    async def _apply_failure_actions(
        self,
        member: discord.Member,
        payload: CaptchaCallbackPayload,
        settings: dict[str, Any],
    ) -> None:
        actions = _normalize_failure_actions(settings.get("captcha-failure-actions"))
        if not actions:
            return

        disciplinary_actions: list[str] = []
        applied_actions: list[str] = []
        notifications: list[str] = []
        log_channel_override: int | None = None
        notify_roles: set[int] = set()

        for action in actions:
            if action.action in {"strike", "kick", "ban"}:
                disciplinary_actions.append(action.action)
                applied_actions.append(action.action)
            elif action.action == "timeout":
                duration = action.extra
                if duration and parse_duration(duration):
                    entry = f"timeout:{duration}"
                    disciplinary_actions.append(entry)
                    applied_actions.append(entry)
                else:
                    _logger.warning(
                        "Ignoring captcha timeout action for guild %s due to invalid duration: %s",
                        member.guild.id,
                        duration,
                    )
            elif action.action == "log":
                notifications.append("log")
                override = _coerce_int(action.extra)
                if override:
                    log_channel_override = override
            elif action.action == "dm_staff":
                notifications.append("dm_staff")
                notify_roles.update(_parse_role_ids(action.extra))
            else:
                _logger.debug(
                    "Unknown captcha failure action '%s' for guild %s", action.action, member.guild.id
                )

        reason = payload.failure_reason or "Failed captcha verification."

        if disciplinary_actions:
            try:
                await strike.perform_disciplinary_action(
                    user=member,
                    bot=self._bot,
                    action_string=disciplinary_actions,
                    reason=reason,
                    source="captcha",
                )
            except discord.Forbidden as exc:
                raise CaptchaProcessingError(
                    "missing_permissions",
                    "Bot is missing permissions to apply captcha failure actions.",
                    http_status=403,
                ) from exc
            except discord.HTTPException as exc:
                raise CaptchaProcessingError(
                    "action_failed",
                    "Failed to apply captcha failure actions due to a Discord API error.",
                ) from exc

        await self._log_failure(
            member,
            payload,
            applied_actions,
            notifications,
            log_channel_override,
            settings,
        )

        if notify_roles:
            await self._notify_staff(member, payload, notify_roles, settings)

    async def _log_failure(
        self,
        member: discord.Member,
        payload: CaptchaCallbackPayload,
        applied_actions: list[str],
        notifications: list[str],
        override_channel: int | None,
        settings: dict[str, Any],
    ) -> None:
        wants_logging = "log" in notifications or override_channel is not None
        if not wants_logging:
            return

        channel_id = override_channel
        if channel_id is None:
            monitor = await mysql.get_settings(member.guild.id, "monitor-channel")
            channel_id = _coerce_int(monitor)

        if not channel_id:
            _logger.debug(
                "Captcha failure logging skipped for guild %s; no log channel configured.",
                member.guild.id,
            )
            return

        attempts = _extract_metadata_int(payload.metadata, "attempts", "attemptCount", "attempt")
        max_attempts = _extract_metadata_int(
            payload.metadata,
            "maxAttempts",
            "max_attempts",
            "attempt_limit",
            "limit",
        )
        if max_attempts is None:
            max_attempts = _coerce_int(settings.get("captcha-max-attempts"))

        embed = discord.Embed(
            title="Captcha Verification Failed",
            description=(
                f"{member.mention} ({member.id}) failed captcha verification."
            ),
            colour=discord.Colour.red(),
        )
        if applied_actions:
            embed.add_field(
                name="Actions Applied",
                value=", ".join(applied_actions),
                inline=False,
            )
        if notifications:
            unique_notifications = sorted({n for n in notifications})
            extras = [n.replace("_", " ").title() for n in unique_notifications]
            embed.add_field(name="Notifications", value=", ".join(extras), inline=False)
        if attempts is not None or max_attempts is not None:
            total = max_attempts if max_attempts is not None else "?"
            used = attempts if attempts is not None else "?"
            embed.add_field(name="Attempts", value=f"{used}/{total}", inline=True)
        challenge = _extract_metadata_str(
            payload.metadata,
            "challenge",
            "challenge_type",
            "challengeType",
            "type",
        )
        if challenge:
            embed.add_field(name="Challenge", value=challenge, inline=True)
        if payload.failure_reason:
            embed.add_field(name="Reason", value=payload.failure_reason, inline=False)

        review_url = _extract_metadata_str(payload.metadata, "reviewUrl", "review_url")
        if review_url:
            embed.add_field(name="Review", value=review_url, inline=False)

        embed.set_footer(text=f"Guild ID: {member.guild.id}")

        try:
            await mod_logging.log_to_channel(embed, channel_id, self._bot)
        except Exception:
            _logger.exception(
                "Failed to send captcha failure log for guild %s to channel %s",
                member.guild.id,
                channel_id,
            )

    async def _notify_staff(
        self,
        member: discord.Member,
        payload: CaptchaCallbackPayload,
        role_ids: set[int],
        settings: dict[str, Any],
    ) -> None:
        recipients: dict[int, discord.Member] = {}
        for role_id in role_ids:
            role = member.guild.get_role(role_id)
            if role is None:
                continue
            for staff_member in role.members:
                if staff_member.id == member.id or staff_member.bot:
                    continue
                recipients[staff_member.id] = staff_member

        if not recipients:
            return

        attempts = _extract_metadata_int(payload.metadata, "attempts", "attemptCount", "attempt")
        max_attempts = _extract_metadata_int(
            payload.metadata,
            "maxAttempts",
            "max_attempts",
            "attempt_limit",
            "limit",
        )
        if max_attempts is None:
            max_attempts = _coerce_int(settings.get("captcha-max-attempts"))

        review_url = _extract_metadata_str(payload.metadata, "reviewUrl", "review_url")

        lines = [
            f"{member.mention} ({member.id}) failed captcha verification in **{member.guild.name}**.",
        ]
        if payload.failure_reason:
            lines.append(f"Reason: {payload.failure_reason}")
        if attempts is not None or max_attempts is not None:
            total = max_attempts if max_attempts is not None else "?"
            used = attempts if attempts is not None else "?"
            lines.append(f"Attempts: {used}/{total}")
        if review_url:
            lines.append(f"Review session: {review_url}")

        message = "\n".join(lines)

        for staff_member in recipients.values():
            try:
                await staff_member.send(message)
            except discord.Forbidden:
                _logger.debug(
                    "Could not DM staff member %s about captcha failure in guild %s",
                    staff_member.id,
                    member.guild.id,
                )
            except discord.HTTPException:
                _logger.debug(
                    "Failed to DM staff member %s about captcha failure in guild %s",
                    staff_member.id,
                    member.guild.id,
                )

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
                f"Member {user_id} not found in guild {guild.id}.",
                http_status=404,
            ) from exc
        except discord.HTTPException as exc:
            raise CaptchaProcessingError(
                "member_fetch_failed",
                f"Failed to fetch member {user_id} from guild {guild.id}.",
            ) from exc


def _filter_roles(
    guild: discord.Guild, role_ids: Iterable[int], member: discord.Member
) -> list[discord.Role]:
    roles: list[discord.Role] = []
    for role_id in role_ids:
        role = guild.get_role(int(role_id))
        if role and role not in member.roles:
            roles.append(role)
    return roles


@dataclass(slots=True)
class FailureAction:
    action: str
    extra: str | None = None


def _normalize_failure_actions(raw: Any) -> list[FailureAction]:
    if not raw:
        return []

    if not isinstance(raw, list):
        raw = [raw]

    normalized: list[FailureAction] = []
    for entry in raw:
        action: str | None = None
        extra: str | None = None

        if isinstance(entry, str):
            action, extra = _split_action(entry)
        elif isinstance(entry, dict):
            action_value = entry.get("value") or entry.get("action") or entry.get("type")
            if isinstance(action_value, str):
                action = action_value.strip().lower() or None
            raw_extra = entry.get("extra") or entry.get("extras") or entry.get("meta")
            if isinstance(raw_extra, dict) and action:
                raw_extra = (
                    raw_extra.get(action)
                    or raw_extra.get("value")
                    or next((str(v) for v in raw_extra.values() if isinstance(v, (str, int))), None)
                )
            if raw_extra is None and action and entry.get(action):
                raw_extra = entry.get(action)
            if raw_extra is not None:
                extra_text = str(raw_extra).strip()
                extra = extra_text or None
        else:
            continue

        if action:
            normalized.append(FailureAction(action=action, extra=extra))

    return normalized


def _split_action(entry: str) -> tuple[str | None, str | None]:
    text = entry.strip()
    if not text:
        return None, None
    action, _, extra = text.partition(":")
    action = action.strip().lower()
    extra = extra.strip() if extra else None
    return action or None, extra or None


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        text = str(value).strip()
        if not text:
            return None
        return int(text)
    except (TypeError, ValueError):
        return None


def _parse_role_ids(value: str | None) -> set[int]:
    if not value:
        return set()
    tokens = str(value).replace("\n", ",").split(",")
    role_ids: set[int] = set()
    for token in tokens:
        number = _coerce_int(token)
        if number:
            role_ids.add(number)
    return role_ids


def _extract_metadata_int(metadata: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        if key in metadata:
            value = metadata.get(key)
            number = _coerce_int(value)
            if number is not None:
                return number
    return None


def _extract_metadata_str(metadata: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None