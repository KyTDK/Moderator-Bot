from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

import discord
from discord.ext import commands
from discord.utils import utcnow

from modules.i18n import get_translated_mapping
from modules.moderation import strike
from modules.utils import mod_logging
from modules.utils import mysql
from modules.utils.time import parse_duration
from modules.utils.localization import TranslateFn, localize_message

from .models import (
    CaptchaCallbackPayload,
    CaptchaProcessingError,
    CaptchaProcessResult,
)
from .sessions import CaptchaSession, CaptchaSessionStore

_logger = logging.getLogger(__name__)


PROCESSOR_BASE_KEY = "modules.captcha.processor"

SUCCESS_TEXTS_FALLBACK: dict[str, Any] = {
    "reason": "Captcha verification successful",
    "embed": {
        "title": "Captcha Verification Passed",
        "description": "{mention} ({user_id}) passed captcha verification.",
        "fields": {
            "actions": "Actions Applied",
            "attempts": "Attempts",
            "provider": "Provider",
            "challenge": "Challenge",
            "review": "Review",
        },
    },
}

FAILURE_TEXTS_FALLBACK: dict[str, Any] = {
    "reason_default": "Failed captcha verification.",
    "notes": {
        "deferred_generic": "Failure actions deferred; captcha attempts remain.",
        "deferred_remaining": "Failure actions deferred; {remaining} {attempts_word} remaining.",
        "member_left": "Member is no longer in the guild; configured failure actions were skipped.",
    },
    "attempts": {
        "unlimited": "Unlimited attempts remain.",
        "additional": "Additional attempts remain.",
        "remaining": "{count} {attempts_word} remaining.",
        "word_one": "attempt",
        "word_other": "attempts",
    },
    "embed": {
        "title_failed": "Captcha Verification Failed",
        "title_attempt": "Captcha Attempt Failed",
        "description_failed": "{mention} ({user_id}) failed captcha verification.",
        "description_attempt": "{mention} ({user_id}) failed a captcha attempt.",
        "fields": {
            "actions": "Actions Applied",
            "notes": "Notes",
            "attempts": "Attempts",
            "remaining": "Attempts Remaining",
            "challenge": "Challenge",
            "reason": "Reason",
            "review": "Review",
        },
    },
}

ERROR_TEXTS_FALLBACK: dict[str, str] = {
    "role_assignment_failed": "Failed to apply captcha success actions due to a Discord API error.",
    "unknown_token": "No pending captcha verification found for this user.",
    "token_mismatch": "Captcha token does not match the pending verification.",
    "missing_permissions": "Bot is missing permissions to apply captcha failure actions.",
    "action_failed": "Failed to apply captcha failure actions due to a Discord API error.",
    "guild_not_found": "Guild {guild_id} is not available to this bot.",
    "guild_fetch_failed": "Failed to fetch guild {guild_id} from Discord.",
    "member_not_found": "Member {user_id} not found in guild {guild_id}.",
    "member_fetch_failed": "Failed to fetch member {user_id} from guild {guild_id}.",
}


def _merge_dicts(base: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in base.items():
        if isinstance(value, Mapping):
            result[key] = _merge_dicts(value, {})
        else:
            result[key] = value
    for key, value in overrides.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), dict):
            result[key] = _merge_dicts(result[key], value)
        else:
            result[key] = value
    return result


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

    def _translator(self) -> TranslateFn | None:
        translator = getattr(self._bot, "translate", None)
        return translator if callable(translator) else None

    def _translate(
        self,
        key: str,
        *,
        fallback: str,
        placeholders: Mapping[str, Any] | None = None,
        guild_id: int | None = None,
    ) -> str:
        return localize_message(
            self._translator(),
            PROCESSOR_BASE_KEY,
            key,
            placeholders=placeholders,
            fallback=fallback,
            guild_id=guild_id,
        )

    def _get_texts(
        self,
        key: str,
        fallback: Mapping[str, Any],
        *,
        guild_id: int | None = None,
    ) -> dict[str, Any]:
        translator = self._translator()
        if translator is not None:
            value = translator(
                f"{PROCESSOR_BASE_KEY}.{key}",
                guild_id=guild_id,
            )
            if isinstance(value, Mapping):
                return _merge_dicts(fallback, value)
        return _merge_dicts(fallback, {})

    async def process(self, payload: CaptchaCallbackPayload) -> CaptchaProcessResult:
        context = await self._build_context(payload)
        session = context.session
        settings = context.settings
        guild = context.guild
        member = context.member

        self._synchronise_session(session, payload)

        if not settings.get("captcha-verification-enabled"):
            _logger.info(
                "Captcha callback ignored because captcha verification is disabled for guild %s",
                guild.id,
            )
            await self._sessions.remove(payload.guild_id, payload.user_id)
            return CaptchaProcessResult(status="disabled", roles_applied=0)

        if not payload.success:
            timeout_failure = self._is_timeout_failure(payload)
            if self._is_grace_period_disabled(settings) and timeout_failure:
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
            exhausted, remaining = self._has_exhausted_attempts(payload, settings)
            if timeout_failure and not exhausted:
                _logger.info(
                    "Treating captcha timeout as verification failure for user %s in guild %s.",
                    member.id,
                    guild.id,
                )
                exhausted = True
                remaining = 0
            await self._apply_failure_actions(
                member,
                payload,
                settings,
                attempts_exhausted=exhausted,
                attempts_remaining=remaining,
            )
            if exhausted:
                _logger.info(
                    "Clearing captcha session for guild %s user %s after attempts were exhausted.",
                    guild.id,
                    member.id,
                )
                await self._sessions.remove(payload.guild_id, payload.user_id)
            else:
                _logger.info(
                    "Captcha session retained for guild %s user %s; attempts remaining: %s",
                    guild.id,
                    member.id,
                    "unknown" if remaining is None else remaining,
                )
            return CaptchaProcessResult(
                status="failed",
                roles_applied=0,
                message=payload.failure_reason,
            )

        action_result: str | None = None
        raw_success_actions = settings.get("captcha-success-actions")
        if raw_success_actions:
            success_texts = self._get_texts("success", SUCCESS_TEXTS_FALLBACK, guild_id=guild.id)
            action_result = await strike.perform_disciplinary_action(
                user=member,
                bot=self._bot,
                action_string=raw_success_actions,
                reason=success_texts["reason"],
                source="captcha",
            )

        if action_result:
            disciplinary_texts = get_translated_mapping(
                self._bot,
                "modules.moderation.strike.disciplinary",
                strike.DISCIPLINARY_TEXTS_FALLBACK,
                guild_id=guild.id,
            )
            failure_prefix = disciplinary_texts["action_failed"].split("{")[0]
            if failure_prefix and failure_prefix in action_result:
                raise CaptchaProcessingError(
                    "role_assignment_failed",
                    self._translate(
                        "errors.role_assignment_failed",
                        fallback=ERROR_TEXTS_FALLBACK["role_assignment_failed"],
                        guild_id=guild.id,
                    ),
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
                _logger.info(
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
                self._translate(
                    "errors.unknown_token",
                    fallback=ERROR_TEXTS_FALLBACK["unknown_token"],
                    guild_id=payload.guild_id,
                ),
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
        _logger.info(
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
                    self._translate(
                        "errors.token_mismatch",
                        fallback=ERROR_TEXTS_FALLBACK["token_mismatch"],
                        guild_id=session.guild_id,
                    ),
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

    def _has_exhausted_attempts(
        self,
        payload: CaptchaCallbackPayload,
        settings: Mapping[str, Any],
    ) -> tuple[bool, int | None]:
        fallback_max_setting = _coerce_int(settings.get("captcha-max-attempts"))
        _, unlimited = _determine_attempt_limit(
            payload.metadata,
            fallback_max_setting,
        )
        if unlimited:
            return False, None

        attempts_remaining = _extract_metadata_int(
            payload.metadata,
            "attemptsRemaining",
            "attempts_remaining",
            "remainingAttempts",
            "remaining_attempts",
            "attemptsLeft",
            "attempts_left",
        )
        if attempts_remaining is not None:
            remaining = max(attempts_remaining, 0)
            return attempts_remaining <= 0, remaining

        attempts, max_attempts = _extract_attempt_counts(
            payload.metadata,
            fallback_max=fallback_max_setting,
        )
        if max_attempts is not None and attempts is not None:
            remaining = max_attempts - attempts
            return remaining <= 0, max(remaining, 0)

        return False, None

    async def _apply_failure_actions(
        self,
        member: discord.Member | _PartialMember,
        payload: CaptchaCallbackPayload,
        settings: dict[str, Any],
        *,
        attempts_exhausted: bool,
        attempts_remaining: int | None,
    ) -> None:
        actions = _normalize_failure_actions(settings.get("captcha-failure-actions"))
        disciplinary_actions: list[str] = []
        failure_texts = self._get_texts(
            "failure",
            FAILURE_TEXTS_FALLBACK,
            guild_id=member.guild.id,
        )
        attempts_texts = failure_texts["attempts"]

        for action in actions:
            if action.action == "log":
                _logger.info(
                    "Ignoring deprecated captcha failure action 'log' for guild %s",
                    member.guild.id,
                )
                continue
            if action.extra:
                disciplinary_actions.append(f"{action.action}:{action.extra}")
            else:
                disciplinary_actions.append(action.action)

        resolved_reason = _resolve_failure_reason(payload) or failure_texts["reason_default"]

        applied_actions: list[str] = []
        skip_note: str | None = None
        if not attempts_exhausted:
            if disciplinary_actions:
                if attempts_remaining is None:
                    skip_note = failure_texts["notes"]["deferred_generic"]
                else:
                    plural_word = (
                        attempts_texts["word_one"]
                        if attempts_remaining == 1
                        else attempts_texts["word_other"]
                    )
                    skip_note = failure_texts["notes"]["deferred_remaining"].format(
                        remaining=attempts_remaining,
                        attempts_word=plural_word,
                    )
                _logger.info(
                    "Deferring captcha failure actions for guild %s user %s; attempts remaining: %s",
                    member.guild.id,
                    member.id,
                    "unknown" if attempts_remaining is None else attempts_remaining,
                )
        elif disciplinary_actions and isinstance(member, discord.Member):
            try:
                await strike.perform_disciplinary_action(
                    user=member,
                    bot=self._bot,
                    action_string=disciplinary_actions,
                    reason=resolved_reason,
                    source="captcha",
                )
            except discord.Forbidden as exc:
                raise CaptchaProcessingError(
                    "missing_permissions",
                    self._translate(
                        "errors.missing_permissions",
                        fallback=ERROR_TEXTS_FALLBACK["missing_permissions"],
                        guild_id=member.guild.id,
                    ),
                    http_status=403,
                ) from exc
            except discord.HTTPException as exc:
                raise CaptchaProcessingError(
                    "action_failed",
                    self._translate(
                        "errors.action_failed",
                        fallback=ERROR_TEXTS_FALLBACK["action_failed"],
                        guild_id=member.guild.id,
                    ),
                ) from exc
            else:
                applied_actions = list(disciplinary_actions)
        elif disciplinary_actions:
            skip_note = failure_texts["notes"]["member_left"]
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
            attempts_exhausted=attempts_exhausted,
            attempts_remaining=attempts_remaining,
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

        success_texts = self._get_texts(
            "success",
            SUCCESS_TEXTS_FALLBACK,
            guild_id=member.guild.id,
        )
        embed_texts = success_texts["embed"]
        embed = discord.Embed(
            title=embed_texts["title"],
            description=embed_texts["description"].format(
                mention=member.mention,
                user_id=member.id,
            ),
            colour=discord.Colour.green(),
        )

        if actions:
            embed.add_field(
                name=embed_texts["fields"]["actions"],
                value=", ".join(actions),
                inline=False,
            )

        fallback_max_attempts = _coerce_int(settings.get("captcha-max-attempts"))
        _, unlimited_attempts = _determine_attempt_limit(
            payload.metadata,
            fallback_max_attempts,
        )
        attempts, max_attempts = _extract_attempt_counts(
            payload.metadata,
            fallback_max=fallback_max_attempts,
        )
        if unlimited_attempts:
            attempts = None
            max_attempts = None

        if (
            not unlimited_attempts
            and (
                (attempts is not None and attempts > 0)
                or (attempts is None and max_attempts is not None)
            )
        ):
            total = max_attempts if max_attempts is not None else "?"
            used = attempts if attempts is not None else "?"
            embed.add_field(
                name=embed_texts["fields"]["attempts"],
                value=f"{used}/{total}",
                inline=True,
            )

        provider = _extract_metadata_str(payload.metadata, "provider")
        if provider:
            embed.add_field(
                name=embed_texts["fields"]["provider"],
                value=provider,
                inline=True,
            )

        challenge = _extract_metadata_str(payload.metadata,"challengeType")
        if challenge:
            embed.add_field(
                name=embed_texts["fields"]["challenge"],
                value=challenge,
                inline=True,
            )

        review_url = _extract_metadata_str(payload.metadata, "reviewUrl", "review_url")
        if review_url:
            embed.add_field(
                name=embed_texts["fields"]["review"],
                value=review_url,
                inline=False,
            )

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
        attempts_exhausted: bool,
        attempts_remaining: int | None,
    ) -> None:
        channel_id = _coerce_int(settings.get("captcha-log-channel"))
        if not channel_id:
            _logger.info(
                "Captcha failure logging skipped for guild %s; no log channel configured.",
                member.guild.id,
            )
            return

        failure_texts = self._get_texts(
            "failure",
            FAILURE_TEXTS_FALLBACK,
            guild_id=member.guild.id,
        )
        embed_texts = failure_texts["embed"]
        attempts_texts = failure_texts["attempts"]
        fallback_max_attempts = _coerce_int(settings.get("captcha-max-attempts"))
        _, unlimited_attempts = _determine_attempt_limit(
            payload.metadata,
            fallback_max_attempts,
        )
        attempts, max_attempts = _extract_attempt_counts(
            payload.metadata,
            fallback_max=fallback_max_attempts,
        )
        if unlimited_attempts:
            attempts = None
            max_attempts = None

        if attempts_exhausted:
            title = embed_texts["title_failed"]
            description = embed_texts["description_failed"].format(
                mention=member.mention,
                user_id=member.id,
            )
            colour = discord.Colour.red()
        else:
            title = embed_texts["title_attempt"]
            description = embed_texts["description_attempt"].format(
                mention=member.mention,
                user_id=member.id,
            )
            colour = discord.Colour.orange()
            if attempts_remaining is None:
                if unlimited_attempts:
                    description += f" {attempts_texts['unlimited']}"
                else:
                    description += f" {attempts_texts['additional']}"
            elif attempts_remaining > 0:
                plural_word = (
                    attempts_texts["word_one"]
                    if attempts_remaining == 1
                    else attempts_texts["word_other"]
                )
                description += " " + attempts_texts["remaining"].format(
                    count=attempts_remaining,
                    attempts_word=plural_word,
                )

        embed = discord.Embed(
            title=title,
            description=description,
            colour=colour,
        )

        if applied_actions:
            embed.add_field(
                name=embed_texts["fields"]["actions"],
                value=", ".join(applied_actions),
                inline=False,
            )
        if note:
            embed.add_field(
                name=embed_texts["fields"]["notes"],
                value=note,
                inline=False,
            )
        if (
            not unlimited_attempts
            and (
                (attempts is not None and attempts > 0)
                or (attempts is None and max_attempts is not None)
            )
        ):
            total = max_attempts if max_attempts is not None else "?"
            used = attempts if attempts is not None else "?"
            embed.add_field(
                name=embed_texts["fields"]["attempts"],
                value=f"{used}/{total}",
                inline=True,
            )
        if (
            not attempts_exhausted
            and not unlimited_attempts
            and attempts_remaining is not None
            and attempts_remaining >= 0
        ):
            embed.add_field(
                name=embed_texts["fields"]["remaining"],
                value=str(attempts_remaining),
                inline=True,
            )
        challenge = _extract_metadata_str(
            payload.metadata,
            "challenge",
            "challenge_type",
            "challengeType",
            "type",
        )
        if challenge:
            embed.add_field(
                name=embed_texts["fields"]["challenge"],
                value=challenge,
                inline=True,
            )
        resolved_reason = _resolve_failure_reason(payload)
        if resolved_reason:
            embed.add_field(
                name=embed_texts["fields"]["reason"],
                value=resolved_reason,
                inline=False,
            )

        review_url = _extract_metadata_str(payload.metadata, "reviewUrl", "review_url")
        if review_url:
            embed.add_field(
                name=embed_texts["fields"]["review"],
                value=review_url,
                inline=False,
            )

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
                self._translate(
                    "errors.guild_not_found",
                    fallback=ERROR_TEXTS_FALLBACK["guild_not_found"],
                    placeholders={"guild_id": guild_id},
                    guild_id=guild_id,
                ),
                http_status=404,
            ) from exc
        except discord.HTTPException as exc:
            raise CaptchaProcessingError(
                "guild_fetch_failed",
                self._translate(
                    "errors.guild_fetch_failed",
                    fallback=ERROR_TEXTS_FALLBACK["guild_fetch_failed"],
                    placeholders={"guild_id": guild_id},
                    guild_id=guild_id,
                ),
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
                self._translate(
                    "errors.member_not_found",
                    fallback=ERROR_TEXTS_FALLBACK["member_not_found"],
                    placeholders={"user_id": user_id, "guild_id": guild.id},
                    guild_id=guild.id,
                ),
                http_status=404,
            ) from exc
        except discord.HTTPException as exc:
            raise CaptchaProcessingError(
                "member_fetch_failed",
                self._translate(
                    "errors.member_fetch_failed",
                    fallback=ERROR_TEXTS_FALLBACK["member_fetch_failed"],
                    placeholders={"user_id": user_id, "guild_id": guild.id},
                    guild_id=guild.id,
                ),
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


def _determine_attempt_limit(
    metadata: Mapping[str, Any],
    fallback: int | None,
) -> tuple[int | None, bool]:
    limit = _extract_metadata_int(
        metadata,
        "maxAttempts",
        "max_attempts",
        "attempt_limit",
        "limit",
    )
    if limit is None:
        limit = fallback
    if limit is None:
        return None, False
    if limit <= 0:
        return None, True
    return limit, False


def _extract_attempt_counts(
    metadata: Mapping[str, Any],
    *,
    fallback_max: int | None = None,
) -> tuple[int | None, int | None]:
    attempts = _extract_metadata_int(
        metadata,
        "failureCount",
        "attempts",
        "attemptCount",
        "attempt",
    )
    attempts_remaining = _extract_metadata_int(
        metadata,
        "attemptsRemaining",
        "attempts_remaining",
        "remainingAttempts",
        "remaining_attempts",
        "attemptsLeft",
        "attempts_left",
    )
    max_attempts, unlimited = _determine_attempt_limit(metadata, fallback_max)

    if attempts is None and max_attempts is not None and attempts_remaining is not None:
        computed = max_attempts - attempts_remaining
        if computed >= 0:
            attempts = computed
            _logger.info(
                "Inferred attempts used (%s) from max attempts (%s) and attempts remaining (%s)",
                attempts,
                max_attempts,
                attempts_remaining,
            )

    if attempts is not None:
        attempts = max(attempts, 0)

    if (
        not unlimited
        and max_attempts is None
        and attempts is not None
        and attempts_remaining is not None
    ):
        computed_total = attempts + attempts_remaining
        if computed_total >= attempts:
            max_attempts = computed_total
            _logger.info(
                "Inferred max attempts (%s) from attempts used (%s) and attempts remaining (%s)",
                max_attempts,
                attempts,
                attempts_remaining,
            )

    return attempts, max_attempts

def _resolve_failure_reason(payload: CaptchaCallbackPayload) -> str | None:
    reason = payload.failure_reason
    if reason:
        return reason
    return _extract_metadata_str(
        payload.metadata,
        "failureReason",
        "failure_reason",
        "failure_message",
        "failureMessage",
        "reason",
    )

def _extract_metadata_str(metadata: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


