from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

import discord
from discord.ext import commands
from discord.utils import utcnow

from modules.utils import mysql
from modules.utils import mod_logging
from modules.moderation import strike
from modules.utils.time import parse_duration

from .models import (
    CaptchaCallbackPayload,
    CaptchaProcessingError,
    CaptchaProcessResult,
)
from .sessions import CaptchaSession, CaptchaSessionStore

_logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _PartialMember:
    guild: discord.Guild
    id: int

    @property
    def mention(self) -> str:
        return f"<@{self.id}>"

@dataclass(slots=True)
class _CaptchaProcessingContext:
    guild: discord.Guild
    member: discord.Member | _PartialMember
    settings: dict[str, Any]
    session: CaptchaSession

class CaptchaCallbackProcessor:
    """Handles captcha callback business logic for a guild member."""

    def __init__(self, bot: commands.Bot, session_store: CaptchaSessionStore):
        self._bot = bot
        self._sessions = session_store

    async def process(self, payload: CaptchaCallbackPayload) -> CaptchaProcessResult:
        context = await self._build_context(payload)
        session = context.session
        settings = context.settings
        guild = context.guild
        member = context.member

        self._synchronise_session(session, payload)

        if not settings.get("captcha-verification-enabled"):
            _logger.debug(
                "Captcha callback ignored because captcha verification is disabled for guild %s",
                guild.id,
            )
            await self._sessions.remove(payload.guild_id, payload.user_id)
            return CaptchaProcessResult(status="disabled", roles_applied=0)

        if not payload.success:
            if self._is_grace_period_disabled(settings) and self._is_timeout_failure(payload):
                _logger.info(
                    "Ignoring captcha timeout for user %s in guild %s because grace period is disabled",
                    member.id,
                    guild.id,
                )
                await self._sessions.remove(payload.guild_id, payload.user_id)
                return CaptchaProcessResult(
                    status="timeout_ignored",
                    roles_applied=0,
                    message=payload.failure_reason,
                )
            _logger.info(
                "Captcha callback reported failure for user %s in guild %s: %s",
                member.id,
                guild.id,
                payload.failure_reason or "unknown reason",
            )
            await self._apply_failure_actions(member, payload, settings)
            await self._sessions.remove(payload.guild_id, payload.user_id)
            return CaptchaProcessResult(
                status="failed",
                roles_applied=0,
                message=payload.failure_reason,
            )

        action_result: str | None = None
        raw_success_actions = settings.get("captcha-success-actions")
        if raw_success_actions:
            action_result = await strike.perform_disciplinary_action(
                user=member,
                bot=self._bot,
                action_string=raw_success_actions,
                reason="Captcha verification successful",
                source="captcha",
            )

        if action_result and "Action failed" in action_result:
            raise CaptchaProcessingError(
                "role_assignment_failed",
                "Failed to apply captcha success actions due to a Discord API error.",
            )

        success_actions = _extract_action_strings(raw_success_actions)

        await self._sessions.remove(payload.guild_id, payload.user_id)

        await self._log_success(
            member,
            payload,
            success_actions,
            settings,
        )

        roles_applied = sum(
            1 for action in success_actions if action.partition(":")[0] == "give_role"
        )

        return CaptchaProcessResult(status="ok", roles_applied=roles_applied)

    async def _build_context(
        self, payload: CaptchaCallbackPayload
    ) -> _CaptchaProcessingContext:
        guild = await self._resolve_guild(payload.guild_id)
        try:
            member = await self._resolve_member(guild, payload.user_id)
        except CaptchaProcessingError as exc:
            if not payload.success and exc.code in {"member_not_found", "member_fetch_failed"}:
                _logger.debug(
                    "Member %s missing when processing failed captcha for guild %s; continuing with partial context.",
                    payload.user_id,
                    payload.guild_id,
                )
                member = _PartialMember(guild=guild, id=payload.user_id)
            else:
                raise
        settings = await mysql.get_settings(
            guild.id,
            [
                "captcha-verification-enabled",
                "captcha-success-actions",
                "captcha-failure-actions",
                "captcha-max-attempts",
                "captcha-log-channel",
                "captcha-delivery-method",
                "captcha-grace-period",
            ],
        )
        session = await self._ensure_session(payload, settings)
        return _CaptchaProcessingContext(
            guild=guild,
            member=member,
            settings=settings,
            session=session,
        )

    async def _ensure_session(
        self,
        payload: CaptchaCallbackPayload,
        settings: Mapping[str, Any],
    ) -> CaptchaSession:
        session = await self._sessions.get(payload.guild_id, payload.user_id)
        if session is not None:
            return session

        method = str(settings.get("captcha-delivery-method") or "dm").strip().lower()
        if method != "embed":
            raise CaptchaProcessingError(
                "unknown_token",
                "No pending captcha verification found for this user.",
                http_status=404,
            )

        embed_session = CaptchaSession(
            guild_id=payload.guild_id,
            user_id=payload.user_id,
            token=payload.token,
            expires_at=self._determine_embed_expiry(settings),
            state=payload.state,
            delivery_method="embed",
        )
        await self._sessions.put(embed_session)
        _logger.debug(
            "Reconstructed embed captcha session for guild %s user %s from callback",
            payload.guild_id,
            payload.user_id,
        )
        return embed_session

    def _synchronise_session(
        self, session: CaptchaSession, payload: CaptchaCallbackPayload
    ) -> None:
        if session.token and session.token != payload.token:
            if session.delivery_method == "embed":
                session.token = payload.token
            else:
                raise CaptchaProcessingError(
                    "token_mismatch",
                    "Captcha token does not match the pending verification.",
                    http_status=404,
                )
        elif not session.token:
            session.token = payload.token

        if payload.state:
            session.state = payload.state

    def _determine_embed_expiry(
        self, settings: Mapping[str, Any]
    ) -> datetime | None:
        raw_grace = settings.get("captcha-grace-period")
        grace = parse_duration(raw_grace) if raw_grace else None
        if grace is None:
            return utcnow() + timedelta(minutes=10)
        if grace.total_seconds() <= 0:
            return None
        return utcnow() + grace

    @staticmethod
    def _is_grace_period_disabled(settings: Mapping[str, Any]) -> bool:
        raw_grace = settings.get("captcha-grace-period")
        if raw_grace is None:
            return False
        if isinstance(raw_grace, (int, float)):
            return raw_grace <= 0
        text = str(raw_grace).strip()
        if not text:
            return False
        grace = parse_duration(text)
        if grace is not None:
            return grace.total_seconds() <= 0
        try:
            numeric = float(text)
        except ValueError:
            return False
        return numeric <= 0

    @staticmethod
    def _is_timeout_failure(payload: CaptchaCallbackPayload) -> bool:
        reason = payload.metadata.get("reason")
        if isinstance(reason, str) and reason.lower() in {"expired", "timeout"}:
            return True
        if payload.metadata.get("timeout"):
            return True
        if isinstance(payload.failure_reason, str) and "timeout" in payload.failure_reason.lower():
            return True
        if payload.status and payload.status.lower() in {"expired", "timeout"}:
            return True
        return False

    async def _apply_failure_actions(
        self,
        member: discord.Member | _PartialMember,
        payload: CaptchaCallbackPayload,
        settings: dict[str, Any],
    ) -> None:
        actions = _normalize_failure_actions(settings.get("captcha-failure-actions"))
        disciplinary_actions: list[str] = []

        for action in actions:
            if action.action == "log":
                _logger.debug(
                    "Ignoring deprecated captcha failure action 'log' for guild %s", member.guild.id
                )
                continue
            if action.extra:
                disciplinary_actions.append(f"{action.action}:{action.extra}")
            else:
                disciplinary_actions.append(action.action)

        reason = payload.failure_reason or "Failed captcha verification."

        applied_actions: list[str] = []
        skip_note: str | None = None
        if disciplinary_actions and isinstance(member, discord.Member):
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
            else:
                applied_actions = list(disciplinary_actions)
        elif disciplinary_actions:
            skip_note = (
                "Member is no longer in the guild; configured failure actions were skipped."
            )
            _logger.info(
                "Skipping captcha failure actions for guild %s user %s because the member is no longer in the guild.",
                member.guild.id,
                member.id,
            )

        await self._log_failure(
            member,
            payload,
            applied_actions,
            settings,
            note=skip_note,
        )

    async def _log_success(
        self,
        member: discord.Member,
        payload: CaptchaCallbackPayload,
        actions: list[str],
        settings: dict[str, Any],
    ) -> None:
        channel_id = _coerce_int(settings.get("captcha-log-channel"))
        if not channel_id:
            return

        embed = discord.Embed(
            title="Captcha Verification Passed",
            description=f"{member.mention} ({member.id}) passed captcha verification.",
            colour=discord.Colour.green(),
        )

        if actions:
            embed.add_field(
                name="Actions Applied",
                value=", ".join(actions),
                inline=False,
            )

        attempts = _extract_metadata_int(payload.metadata, "failureCount")
        max_attempts = _extract_metadata_int(payload.metadata, "maxAttempts")
        if attempts is not None or max_attempts is not None:
            total = max_attempts if max_attempts is not None else "?"
            used = attempts if attempts is not None else "?"
            embed.add_field(name="Attempts", value=f"{used}/{total}", inline=True)

        provider = _extract_metadata_str(payload.metadata, "provider")
        if provider:
            embed.add_field(name="Provider", value=provider, inline=True)
            
        challenge = _extract_metadata_str(payload.metadata,"challengeType")
        if challenge:
            embed.add_field(name="Challenge", value=challenge, inline=True)

        review_url = _extract_metadata_str(payload.metadata, "reviewUrl", "review_url")
        if review_url:
            embed.add_field(name="Review", value=review_url, inline=False)

        embed.set_footer(text=f"Guild ID: {member.guild.id}")

        try:
            await mod_logging.log_to_channel(embed, channel_id, self._bot)
        except Exception:
            _logger.exception(
                "Failed to send captcha success log for guild %s to channel %s",
                member.guild.id,
                channel_id,
            )

    async def _log_failure(
        self,
        member: discord.Member | _PartialMember,
        payload: CaptchaCallbackPayload,
        applied_actions: list[str],
        settings: dict[str, Any],
        *,
        note: str | None = None,
    ) -> None:
        channel_id = _coerce_int(settings.get("captcha-log-channel"))
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
        if note:
            embed.add_field(name="Notes", value=note, inline=False)
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


def _extract_action_strings(raw: Any) -> list[str]:
    if raw is None:
        return []

    entries: Iterable[Any]
    if isinstance(raw, str):
        entries = [raw]
    elif isinstance(raw, Iterable):
        entries = raw
    else:
        entries = [raw]

    result: list[str] = []
    for entry in entries:
        if isinstance(entry, str):
            text = entry.strip()
            if text:
                result.append(text)

    return result


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
