from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Mapping

import discord
from discord.ext import commands
from discord.utils import utcnow

from modules.i18n import get_translated_mapping
from modules.moderation import strike
from modules.utils import mod_logging
from modules.utils.localization import TranslateFn, localize_message
from modules.utils.time import parse_duration
from modules.verification.actions import apply_role_actions, parse_role_actions

from ..models import (
    CaptchaCallbackPayload,
    CaptchaProcessingError,
    CaptchaProcessResult,
)
from ..sessions import CaptchaSession, CaptchaSessionStore
from .constants import (
    ERROR_TEXTS_FALLBACK,
    FAILURE_TEXTS_FALLBACK,
    PROCESSOR_BASE_KEY,
    SUCCESS_TEXTS_FALLBACK,
)
from .contexts import _CaptchaProcessingContext, _PartialMember, _VpnPolicyContext
from .helpers import (
    _coerce_bool,
    _coerce_float,
    _coerce_int,
    _coerce_mapping,
    _coerce_string_list,
    _determine_attempt_limit,
    _extract_action_strings,
    _extract_attempt_counts,
    _extract_metadata_int,
    _extract_metadata_str,
    _merge_dicts,
    _mysql_module,
    _normalize_failure_actions,
    _resolve_failure_reason,
    _sanitize_policy_actions,
    _sanitize_policy_behavior,
    _sanitize_policy_providers,
    _truncate,
)

_logger = logging.getLogger("modules.captcha.processor")

class CaptchaCallbackProcessor:
    """Handles captcha callback business logic for a guild member."""

    def __init__(self, bot: commands.Bot, session_store: CaptchaSessionStore):
        self._bot = bot
        self._sessions = session_store
        self._vpn_action_history: dict[str, dict[str, float]] = {}

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

    async def process(
        self,
        payload: CaptchaCallbackPayload,
        *,
        message_id: str | None = None,
    ) -> CaptchaProcessResult:
        context = await self._build_context(payload)
        session = context.session
        settings = context.settings
        guild = context.guild
        member = context.member

        self._synchronise_session(session, payload)

        policy = self._extract_vpn_policy(payload)
        if policy and session is not None:
            self._record_vpn_policy(session, payload, policy, message_id)
            if policy.reason and not payload.failure_reason:
                payload.failure_reason = policy.reason

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
            if policy:
                decision = policy.decision.lower()
                if decision == "deny":
                    exhausted = True
                    remaining = 0
                elif decision == "challenge":
                    exhausted = False
            await self._apply_failure_actions(
                member,
                payload,
                settings,
                attempts_exhausted=exhausted,
                attempts_remaining=remaining,
                policy=policy,
                message_id=message_id,
                session=session,
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

        success_texts = self._get_texts(
            "success",
            SUCCESS_TEXTS_FALLBACK,
            guild_id=guild.id,
        )
        action_result: str | None = None
        raw_success_actions = settings.get("captcha-success-actions")
        if raw_success_actions:
            action_result = await strike.perform_disciplinary_action(
                user=member,
                bot=self._bot,
                action_string=raw_success_actions,
                reason=success_texts["reason"],
                source="captcha",
            )

        vpn_post_actions = parse_role_actions(settings.get("vpn-post-actions"))
        vpn_post_applied: list[str] = []
        if vpn_post_actions and isinstance(member, discord.Member):
            vpn_reason = success_texts.get("vpn_post_reason")
            if not isinstance(vpn_reason, str) or not vpn_reason.strip():
                vpn_reason = SUCCESS_TEXTS_FALLBACK.get(
                    "vpn_post_reason",
                    "Applying VPN post-verification role adjustments.",
                )
            vpn_post_applied = await apply_role_actions(
                member,
                vpn_post_actions,
                reason=vpn_reason,
                logger=_logger,
            )
            if vpn_post_applied:
                _logger.info(
                    "Applied VPN post-actions for guild %s user %s: %s",
                    guild.id,
                    member.id,
                    ", ".join(vpn_post_applied),
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
        if vpn_post_applied:
            success_actions.extend(vpn_post_applied)

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
        mysql = _mysql_module()
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
                "vpn-detection-actions",
                "vpn-post-actions",
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
        policy: _VpnPolicyContext | None,
        message_id: str | None,
        session: CaptchaSession | None,
    ) -> None:
        raw_actions: Any
        if policy and policy.actions:
            raw_actions = policy.actions
        elif policy:
            raw_actions = settings.get("vpn-detection-actions") or settings.get(
                "captcha-failure-actions"
            )
        else:
            raw_actions = settings.get("captcha-failure-actions")

        actions = _normalize_failure_actions(raw_actions)
        disciplinary_actions: list[str] = []
        failure_texts = self._get_texts(
            "failure",
            FAILURE_TEXTS_FALLBACK,
            guild_id=member.guild.id,
        )
        attempts_texts = failure_texts["attempts"]
        policy_requires_challenge = False
        executed_action_strings: list[str] = []

        for action in actions:
            normalized = action.action
            if normalized == "log":
                _logger.info(
                    "Ignoring deprecated captcha failure action 'log' for guild %s",
                    member.guild.id,
                )
                continue
            action_string = f"{normalized}:{action.extra}" if action.extra else normalized
            if normalized == "challenge":
                policy_requires_challenge = True
                executed_action_strings.append(action_string)
                continue
            disciplinary_actions.append(action_string)
            executed_action_strings.append(action_string)

        policy_actions_fallback = bool(
            policy is not None and not policy.actions and executed_action_strings
        )
        if policy is not None:
            if policy_actions_fallback:
                policy.actions = list(executed_action_strings)
            policy.requires_challenge = policy_requires_challenge
            if policy_actions_fallback and session is not None:
                storage = session.metadata.setdefault("vpn_detection", {})
                storage["actions"] = list(policy.actions)

        resolved_reason = (
            (policy.reason if policy and policy.reason else None)
            or _resolve_failure_reason(payload)
            or failure_texts["reason_default"]
        )

        applied_actions: list[str] = []
        notes: list[str] = []
        force_execute = bool(policy and policy.decision.lower() == "deny")
        if policy and policy.decision.lower() == "challenge" and disciplinary_actions:
            force_execute = True

        def _append_note(text: str | None) -> None:
            if text and text not in notes:
                notes.append(text)

        if policy_actions_fallback:
            _append_note(failure_texts["notes"].get("policy_actions_fallback"))

        if not attempts_exhausted and not force_execute:
            if disciplinary_actions:
                if attempts_remaining is None:
                    _append_note(failure_texts["notes"]["deferred_generic"])
                else:
                    plural_word = (
                        attempts_texts["word_one"]
                        if attempts_remaining == 1
                        else attempts_texts["word_other"]
                    )
                    _append_note(
                        failure_texts["notes"]["deferred_remaining"].format(
                            remaining=attempts_remaining,
                            attempts_word=plural_word,
                        )
                    )
                _logger.info(
                    "Deferring captcha failure actions for guild %s user %s; attempts remaining: %s",
                    member.guild.id,
                    member.id,
                    "unknown" if attempts_remaining is None else attempts_remaining,
                )
        elif disciplinary_actions and isinstance(member, discord.Member):
            should_execute = True
            dedupe_key: str | None = None
            if policy:
                should_execute, dedupe_key = self._mark_policy_message(
                    session,
                    payload,
                    policy,
                    message_id,
                )
                if not should_execute and dedupe_key:
                    _logger.info(
                        "Skipping duplicate VPN policy actions for guild %s user %s (key=%s)",
                        member.guild.id,
                        member.id,
                        dedupe_key,
                    )
            if should_execute:
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
            else:
                _append_note(failure_texts["notes"].get("policy_duplicate"))
        elif disciplinary_actions:
            _append_note(failure_texts["notes"]["member_left"])
            _logger.info(
                "Skipping captcha failure actions for guild %s user %s because the member is no longer in the guild.",
                member.guild.id,
                member.id,
            )

        if policy_requires_challenge:
            if policy and policy.escalation:
                _append_note(
                    failure_texts["notes"]["policy_challenge"].format(
                        escalation=policy.escalation,
                    )
                )
            else:
                _append_note(failure_texts["notes"]["policy_challenge_generic"])

        if policy and policy.cached_state == "stale":
            _append_note(failure_texts["notes"].get("vpn_cached_stale"))

        await self._log_failure(
            member,
            payload,
            applied_actions,
            settings,
            note="\n".join(notes) if notes else None,
            attempts_exhausted=attempts_exhausted,
            attempts_remaining=attempts_remaining,
            policy=policy,
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
        policy: _VpnPolicyContext | None = None,
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
        resolved_reason = (
            (policy.reason if policy and policy.reason else None)
            or _resolve_failure_reason(payload)
        )
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

        if policy is not None:
            fields = embed_texts["fields"]
            decision_label = policy.decision.replace("_", " ").title() if policy.decision else "Unknown"
            policy_lines = [decision_label]
            if policy.source:
                policy_lines.append(f"Source: {policy.source}")
            if policy.escalation:
                policy_lines.append(f"Escalation: {policy.escalation}")
            embed.add_field(
                name=fields.get("policy", "VPN Screening"),
                value=_truncate("\n".join(policy_lines), 256),
                inline=True,
            )

            risk_parts: list[str] = []
            if policy.risk_score is not None:
                risk_parts.append(f"score={policy.risk_score:.1f}" if isinstance(policy.risk_score, float) else f"score={policy.risk_score}")
            if policy.providers_flagged is not None or policy.provider_count is not None:
                flagged = policy.providers_flagged if policy.providers_flagged is not None else 0
                count_text: str | int = policy.provider_count if policy.provider_count is not None else "?"
                risk_parts.append(f"providers={flagged}/{count_text}")
            risk_text = ", ".join(risk_parts)
            if risk_text:
                embed.add_field(
                    name=fields.get("risk", "VPN Risk"),
                    value=risk_text,
                    inline=True,
                )

            provider_summary = self._format_policy_providers(policy.providers)
            if provider_summary:
                embed.add_field(
                    name=fields.get("providers", "Providers"),
                    value=provider_summary,
                    inline=False,
                )

            behavior_summary = self._format_policy_behavior(policy.behavior)
            if behavior_summary:
                embed.add_field(
                    name=fields.get("behavior", "Behaviour Signals"),
                    value=behavior_summary,
                    inline=False,
                )

            if policy.cached_state:
                cached_label = policy.cached_state.title() if policy.cached_state else policy.cached_state
                embed.add_field(
                    name=fields.get("cached_state", "Intel Cache"),
                    value=cached_label,
                    inline=True,
                )

            if policy.hard_signals:
                embed.add_field(
                    name=fields.get("hard_signals", "Hard Signals"),
                    value=_truncate(", ".join(policy.hard_signals), 1024),
                    inline=False,
                )

            if policy.actions:
                embed.add_field(
                    name=fields.get("policy_actions", "Policy Actions"),
                    value=_truncate(", ".join(policy.actions), 1024),
                    inline=False,
                )

            flagged_at = policy.flagged_at or policy.timestamp
            if flagged_at:
                try:
                    timestamp_value = max(0, int(int(flagged_at) / 1000))
                except (TypeError, ValueError):
                    timestamp_value = None
                if timestamp_value is not None:
                    embed.add_field(
                        name=fields.get("flagged_at", "Flagged At"),
                        value=f"<t:{timestamp_value}:F>\n<t:{timestamp_value}:R>",
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

    def _format_policy_providers(
        self, providers: list[dict[str, Any]]
    ) -> str | None:
        if not providers:
            return None
        lines: list[str] = []
        for provider in providers:
            if not isinstance(provider, Mapping):
                provider = dict(provider)
            name_raw = provider.get("provider")
            name = str(name_raw).strip() if isinstance(name_raw, str) else None
            if not name:
                name = "unknown"
            parts = [name]
            flagged = provider.get("flagged")
            if isinstance(flagged, bool):
                parts.append("flagged" if flagged else "clear")
            vpn_flags: list[str] = []
            if _coerce_bool(provider.get("isVpn")):
                vpn_flags.append("vpn")
            if _coerce_bool(provider.get("isProxy")):
                vpn_flags.append("proxy")
            if _coerce_bool(provider.get("isTor")):
                vpn_flags.append("tor")
            if vpn_flags:
                parts.append(f"({'/'.join(vpn_flags)})")
            risk = provider.get("risk")
            if isinstance(risk, (int, float)):
                risk_text = f"risk={risk:.1f}" if isinstance(risk, float) else f"risk={risk}"
                parts.append(risk_text)
            lines.append(" ".join(parts))
        summary = "\n".join(lines)
        return _truncate(summary, 1024) if summary else None

    def _format_policy_behavior(self, behavior: Mapping[str, Any]) -> str | None:
        if not behavior:
            return None
        parts: list[str] = []
        for key, value in behavior.items():
            if not isinstance(key, str):
                continue
            label = key.strip()
            if not label:
                continue
            if isinstance(value, (int, float)):
                parts.append(f"{label}={value}")
            elif isinstance(value, str):
                text = value.strip()
                if text:
                    parts.append(f"{label}={text}")
            elif isinstance(value, bool):
                parts.append(f"{label}={'true' if value else 'false'}")
        summary = ", ".join(parts[:8])
        return _truncate(summary, 1024) if summary else None

    def _extract_vpn_policy(
        self, payload: CaptchaCallbackPayload
    ) -> _VpnPolicyContext | None:
        metadata = payload.metadata
        source_raw = metadata.get("policySource") or metadata.get("policy_source")
        if not isinstance(source_raw, str):
            return None
        source = source_raw.strip()
        if source.lower() != "vpn-detection":
            return None

        detail = _coerce_mapping(
            metadata.get("policyDetail") or metadata.get("policy_detail")
        )
        if detail is None:
            return None

        decision_raw = detail.get("decision")
        decision = (
            str(decision_raw).strip().lower()
            if isinstance(decision_raw, str)
            else ""
        )
        if not decision:
            decision = "unknown"

        reason = detail.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            reason = None
        else:
            reason = reason.strip()

        risk_score = _coerce_float(detail.get("riskScore") or detail.get("risk_score"))
        provider_count = _coerce_int(
            detail.get("providerCount") or detail.get("provider_count")
        )
        providers_flagged = _coerce_int(
            detail.get("providersFlagged") or detail.get("providers_flagged")
        )
        cached_state_raw = detail.get("cachedState") or detail.get("cached_state")
        cached_state = (
            str(cached_state_raw).strip().lower()
            if isinstance(cached_state_raw, str)
            else None
        )
        escalation_raw = detail.get("escalation")
        escalation = (
            str(escalation_raw).strip()
            if isinstance(escalation_raw, str) and escalation_raw.strip()
            else None
        )
        timestamp = _coerce_int(detail.get("timestamp"))
        flagged_at = _coerce_int(detail.get("flaggedAt") or detail.get("flagged_at"))
        hard_signals = _coerce_string_list(
            detail.get("hardSignals") or detail.get("hard_signals")
        )
        providers = _sanitize_policy_providers(detail.get("providers"))
        behavior = _sanitize_policy_behavior(detail.get("behavior"))
        actions = _sanitize_policy_actions(detail.get("actions"))

        policy = _VpnPolicyContext(
            source=source,
            decision=decision,
            actions=actions,
            reason=reason,
            risk_score=risk_score,
            provider_count=provider_count,
            providers_flagged=providers_flagged,
            providers=providers,
            behavior=behavior,
            hard_signals=hard_signals,
            cached_state=cached_state,
            escalation=escalation,
            timestamp=timestamp,
            flagged_at=flagged_at,
        )
        policy.requires_challenge = any(
            action.split(":", 1)[0].strip().lower() == "challenge" for action in actions
        )
        return policy

    def _record_vpn_policy(
        self,
        session: CaptchaSession,
        payload: CaptchaCallbackPayload,
        policy: _VpnPolicyContext,
        message_id: str | None,
    ) -> None:
        storage = session.metadata.setdefault("vpn_detection", {})
        storage["source"] = policy.source
        storage["decision"] = policy.decision
        if policy.reason:
            storage["reason"] = policy.reason
        if policy.risk_score is not None:
            storage["risk_score"] = policy.risk_score
        if policy.provider_count is not None:
            storage["provider_count"] = policy.provider_count
        if policy.providers_flagged is not None:
            storage["providers_flagged"] = policy.providers_flagged
        if policy.behavior:
            storage["behavior"] = policy.behavior
        if policy.hard_signals:
            storage["hard_signals"] = policy.hard_signals
        if policy.cached_state:
            storage["cached_state"] = policy.cached_state
        if policy.actions:
            storage["actions"] = policy.actions
        if policy.escalation:
            storage["escalation"] = policy.escalation
        if policy.providers:
            storage["providers"] = policy.providers
        if message_id:
            storage["last_message_id"] = message_id
        storage["last_token"] = payload.token

        flagged_at = policy.flagged_at or policy.timestamp
        if flagged_at is None:
            flagged_at = storage.get("flagged_at")
        if flagged_at is None:
            flagged_at = int(utcnow().timestamp() * 1000)
        storage["flagged_at"] = flagged_at

        history = storage.get("history")
        if not isinstance(history, list):
            history = []
        entry: dict[str, Any] = {
            "decision": policy.decision,
            "reason": policy.reason,
            "risk_score": policy.risk_score,
            "providers_flagged": policy.providers_flagged,
            "provider_count": policy.provider_count,
            "cached_state": policy.cached_state,
            "hard_signals": policy.hard_signals,
            "actions": policy.actions,
            "escalation": policy.escalation,
            "timestamp": policy.timestamp,
            "flagged_at": flagged_at,
            "message_id": message_id,
        }
        if policy.behavior:
            entry["behavior"] = policy.behavior
        if policy.providers:
            entry["providers"] = policy.providers
        history.append(entry)
        storage["history"] = history[-10:]

    def _mark_policy_message(
        self,
        session: CaptchaSession | None,
        payload: CaptchaCallbackPayload,
        policy: _VpnPolicyContext,
        message_id: str | None,
    ) -> tuple[bool, str | None]:
        dedupe_key = (
            message_id
            or str(payload.metadata.get("messageId") or "").strip()
            or payload.token
        )
        if not dedupe_key:
            return True, None

        processed_set: set[str] = set()
        storage: dict[str, Any] | None = None
        if session is not None:
            storage = session.metadata.setdefault("vpn_detection", {})
            processed = storage.get("processed_ids")
            if isinstance(processed, list):
                processed_set = set(processed)
            elif isinstance(processed, set):
                processed_set = processed

        token_key = payload.token or f"{payload.guild_id}:{payload.user_id}"
        history = self._vpn_action_history.setdefault(token_key, {})

        if dedupe_key in processed_set or dedupe_key in history:
            if storage is not None:
                storage["processed_ids"] = processed_set
            history[dedupe_key] = history.get(dedupe_key, utcnow().timestamp())
            return False, dedupe_key

        processed_set.add(dedupe_key)
        if storage is not None:
            storage["processed_ids"] = processed_set

        history[dedupe_key] = utcnow().timestamp()
        if len(history) > 32:
            excess = sorted(history.items(), key=lambda item: item[1])[: len(history) - 32]
            for key, _ in excess:
                history.pop(key, None)

        return True, dedupe_key

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


