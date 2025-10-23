from __future__ import annotations

import importlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

_logger = logging.getLogger("modules.captcha.processor")


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


def _truncate(text: str, limit: int = 1024) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


def _coerce_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if isinstance(value, str):
        try:
            return float(value.strip())
        except (TypeError, ValueError):
            return None
    return None


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return None


def _coerce_string_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, Iterable):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text:
            continue
        lowered = text.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        result.append(text)
    return result


def _coerce_mapping(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, Mapping):
            return dict(parsed)
    return None


def _sanitize_policy_actions(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    elif not isinstance(raw, Iterable):
        raw = [raw]
    actions: list[str] = []
    seen: set[str] = set()
    for entry in raw:
        text: str | None = None
        if isinstance(entry, str):
            text = entry.strip()
        elif isinstance(entry, Mapping):
            candidate = entry.get("action") or entry.get("value") or entry.get("type")
            if isinstance(candidate, str):
                text = candidate.strip()
        if not text:
            continue
        lowered = text.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        actions.append(text)
    return actions


def _sanitize_policy_providers(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, Iterable) or isinstance(raw, (str, bytes)):
        return []
    providers: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        provider: dict[str, Any] = {}
        name = entry.get("provider")
        if isinstance(name, str) and name.strip():
            provider["provider"] = name.strip()
        flagged = _coerce_bool(entry.get("flagged") or entry.get("isFlagged"))
        if flagged is not None:
            provider["flagged"] = flagged
        vpn = _coerce_bool(entry.get("isVpn") or entry.get("is_vpn"))
        if vpn is not None:
            provider["isVpn"] = vpn
        proxy = _coerce_bool(entry.get("isProxy") or entry.get("is_proxy"))
        if proxy is not None:
            provider["isProxy"] = proxy
        tor = _coerce_bool(entry.get("isTor") or entry.get("is_tor"))
        if tor is not None:
            provider["isTor"] = tor
        risk = _coerce_float(entry.get("risk") or entry.get("score"))
        if risk is not None:
            provider["risk"] = risk
        if provider:
            providers.append(provider)
    return providers


def _sanitize_policy_behavior(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {}
    behavior: dict[str, Any] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            continue
        label = key.strip()
        if not label:
            continue
        if isinstance(value, (int, float)):
            behavior[label] = value
        elif isinstance(value, str):
            text = value.strip()
            if text:
                behavior[label] = text
        elif isinstance(value, bool):
            behavior[label] = value
    return behavior


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


def _resolve_failure_reason(payload: Any) -> str | None:
    reason = getattr(payload, "failure_reason", None)
    if reason:
        return reason
    metadata = getattr(payload, "metadata", None)
    if not isinstance(metadata, Mapping):
        return None
    return _extract_metadata_str(
        metadata,
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


def _mysql_module():
    return importlib.import_module("modules.utils.mysql")


__all__ = [
    "FailureAction",
    "_merge_dicts",
    "_truncate",
    "_coerce_float",
    "_coerce_bool",
    "_coerce_string_list",
    "_coerce_mapping",
    "_sanitize_policy_actions",
    "_sanitize_policy_providers",
    "_sanitize_policy_behavior",
    "_normalize_failure_actions",
    "_extract_action_strings",
    "_split_action",
    "_coerce_int",
    "_extract_metadata_int",
    "_determine_attempt_limit",
    "_extract_attempt_counts",
    "_resolve_failure_reason",
    "_extract_metadata_str",
    "_mysql_module",
]
