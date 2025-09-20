from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

_TRUE_VALUES = {"1", "true", "yes", "on", "success", "passed", "complete", "completed"}
_FALSE_VALUES = {"0", "false", "no", "off", "failed", "failure"}

class CaptchaPayloadError(ValueError):
    """Raised when the incoming webhook payload is invalid."""

@dataclass(slots=True)
class CaptchaCallbackPayload:
    guild_id: int
    user_id: int
    success: bool
    request_id: str | None
    failure_reason: str | None
    metadata: dict[str, Any]

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "CaptchaCallbackPayload":
        try:
            raw_guild = data["guild_id"]
            raw_user = data["user_id"]
        except KeyError as exc:  # pragma: no cover - defensive guard
            raise CaptchaPayloadError(f"Missing required field: {exc.args[0]}") from exc

        try:
            guild_id = int(raw_guild)
            user_id = int(raw_user)
        except (TypeError, ValueError) as exc:
            raise CaptchaPayloadError("guild_id and user_id must be integers") from exc

        success = _coerce_success(data)
        failure_reason = data.get("failure_reason") or data.get("reason")
        request_id = _coerce_request_id(data)

        metadata = {
            key: value
            for key, value in data.items()
            if key not in {"guild_id", "user_id", "success", "status", "result", "passed", "failure_reason", "reason", "request_id", "session_id", "metadata"}
        }

        # Allow nested metadata dict provided as "metadata"
        nested_meta = data.get("metadata")
        if isinstance(nested_meta, Mapping):
            metadata.update(dict(nested_meta))

        return cls(
            guild_id=guild_id,
            user_id=user_id,
            success=success,
            request_id=request_id,
            failure_reason=failure_reason,
            metadata=metadata,
        )

def _coerce_success(data: Mapping[str, Any]) -> bool:
    if "success" in data:
        return _to_bool(data["success"])
    if "passed" in data:
        return _to_bool(data["passed"])
    for key in ("status", "result", "outcome"):
        if key in data:
            return _to_bool(data[key])
    # Default to False if explicitly absent to avoid granting roles accidentally
    return False

def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE_VALUES:
            return True
        if lowered in _FALSE_VALUES:
            return False
    return False

def _coerce_request_id(data: Mapping[str, Any]) -> str | None:
    for key in ("request_id", "session_id", "challenge_id", "verification_id"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return None

@dataclass(slots=True)
class CaptchaWebhookResult:
    status: str
    roles_applied: int
    message: str | None = None

class CaptchaProcessingError(Exception):
    """Raised when the captcha callback cannot be processed."""

    def __init__(self, code: str, message: str, *, http_status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.message = message
