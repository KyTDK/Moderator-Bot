from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Iterable

from discord.ext import commands

try:  # pragma: no cover - optional dependency
    from redis.exceptions import ConnectionError as RedisConnectionError
    from redis.exceptions import ResponseError
except ModuleNotFoundError:  # pragma: no cover - handled gracefully
    RedisConnectionError = Exception  # type: ignore[assignment]
    ResponseError = Exception  # type: ignore[assignment]

from modules.captcha.config import CaptchaStreamConfig
from modules.captcha.models import (
    CaptchaCallbackPayload,
    CaptchaPayloadError,
    CaptchaProcessingError,
    CaptchaSettingsUpdatePayload,
)
from modules.captcha.processor import CaptchaCallbackProcessor
from modules.captcha.sessions import CaptchaSessionStore
from modules.utils.redis_stream import (
    RedisStreamConsumer,
    RedisStreamMessage,
    decode_if_bytes,
    normalize_stream_fields,
    redis_from_url as _redis_from_url,
)

redis_from_url = _redis_from_url

if TYPE_CHECKING:  # pragma: no cover - type narrow
    from redis.asyncio import Redis as RedisClient

_logger = logging.getLogger(__name__)

SettingsUpdateCallback = Callable[[CaptchaSettingsUpdatePayload], Awaitable[None] | None]

__all__ = ["CaptchaStreamListener", "RedisConnectionError", "redis_from_url"]


class CaptchaStreamListener(RedisStreamConsumer):
    """Consumes captcha callbacks from Redis and dispatches them to processors."""

    def __init__(
        self,
        bot: commands.Bot,
        config: CaptchaStreamConfig,
        session_store: CaptchaSessionStore,
        settings_update_callback: SettingsUpdateCallback | None = None,
    ) -> None:
        super().__init__(config, logger=_logger, redis_factory=redis_from_url)
        self._bot = bot
        self._config_ext = config
        self._processor = CaptchaCallbackProcessor(bot, session_store)
        self._settings_callback = settings_update_callback
        self._last_message_id: str = config.start_id
        self._supports_xautoclaim = True
        self._autoclaim_warning_emitted = False

    async def reclaim_pending(self, redis: RedisClient) -> None:  # type: ignore[override]
        if self._config_ext.pending_auto_claim_ms <= 0 or not self._supports_xautoclaim:
            return

        next_id = "0-0"
        try:
            while True:
                next_id, messages = self._normalize_xautoclaim_response(
                    await redis.xautoclaim(  # type: ignore[attr-defined]
                        self._config.stream,
                        self._config.group,
                        self._config.consumer_name,
                        self._config_ext.pending_auto_claim_ms,
                        next_id,
                        count=self._config.fetch_count,
                    )
                )
                if not messages:
                    break
                for message_id, fields in messages:
                    message = RedisStreamMessage(
                        stream=self._config.stream,
                        message_id=message_id,
                        fields=normalize_stream_fields(fields),
                    )
                    await self._handle_single(message)
                if next_id in {"0", "0-0"}:
                    break
        except ResponseError as exc:
            if "XAUTOCLAIM" in str(exc).upper():
                if not self._autoclaim_warning_emitted:
                    _logger.warning(
                        "Redis server does not support XAUTOCLAIM; pending captcha callbacks may take longer to recover.",
                    )
                    self._autoclaim_warning_emitted = True
                self._supports_xautoclaim = False
            else:  # pragma: no cover - unexpected redis errors
                raise
        except AttributeError:  # pragma: no cover - redis version mismatch
            if not self._autoclaim_warning_emitted:
                _logger.warning(
                    "redis-py does not expose XAUTOCLAIM; pending captcha callbacks may take longer to recover.",
                )
                self._autoclaim_warning_emitted = True
            self._supports_xautoclaim = False

    async def handle_message(self, message: RedisStreamMessage) -> bool:  # noqa: D401
        fields = message.fields
        self._last_message_id = message.message_id

        _logger.info(
            "[captcha/callback] received redis entry %s/%s: fields=%r",
            message.stream,
            message.message_id,
            fields,
        )

        payload_raw = fields.get("payload")
        if not payload_raw:
            _logger.warning("Captcha callback %s missing payload; acknowledging", message.message_id)
            return True

        payload_text = self._coerce_text(payload_raw)
        _logger.info(
            "[captcha/callback] payload text for %s/%s: %s",
            message.stream,
            message.message_id,
            payload_text,
        )

        if self._config_ext.shared_secret:
            signature = fields.get("signature")
            signature_text = self._coerce_text(signature) if signature is not None else ""
            expected = hmac.new(
                self._config_ext.shared_secret,
                payload_text.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            if not signature_text or not hmac.compare_digest(signature_text.lower(), expected.lower()):
                _logger.warning("Captcha callback %s has invalid signature; discarding", message.message_id)
                return True

        try:
            payload_dict = json.loads(payload_text)
        except json.JSONDecodeError:
            _logger.warning("Captcha callback %s contains invalid JSON payload", message.message_id)
            return True

        event_type_raw = payload_dict.get("eventType") or payload_dict.get("event_type")
        event_type = str(event_type_raw).strip() if isinstance(event_type_raw, str) else ""

        if event_type == "captcha.settings.updated":
            return await self._handle_settings_update_event(message.message_id, payload_dict)

        if event_type and event_type not in {
            "captcha.verification.completed",
            "captcha.verification.failed",
        }:
            _logger.info("Ignoring unknown captcha event type %s", event_type)
            return True

        return await self._handle_verification_event(message.message_id, payload_dict, fields)

    async def _handle_verification_event(
        self,
        message_id: str,
        payload_dict: dict[str, Any],
        fields: dict[str, Any],
    ) -> bool:
        try:
            payload = CaptchaCallbackPayload.from_mapping(payload_dict)
        except CaptchaPayloadError as exc:
            _logger.warning("Captcha callback %s rejected: %s", message_id, exc)
            return True

        guild = self._bot.get_guild(payload.guild_id)
        if guild is None:
            _logger.info(
                "Skipping captcha callback %s for guild %s; not managed by this instance",
                message_id,
                payload.guild_id,
            )
            await self._requeue(fields)
            return True

        try:
            await self._processor.process(payload, message_id=message_id)
            _logger.info(
                "Processed captcha callback for guild %s user %s (message %s)",
                payload.guild_id,
                payload.user_id,
                message_id,
            )
        except CaptchaProcessingError as exc:
            _logger.info(
                "Captcha callback %s failed for guild %s user %s: %s",
                message_id,
                payload.guild_id,
                payload.user_id,
                exc,
            )
        except Exception:
            _logger.exception(
                "Unhandled error while processing captcha callback %s for guild %s user %s",
                message_id,
                payload.guild_id,
                payload.user_id,
            )
        return True

    async def _handle_settings_update_event(
        self,
        message_id: str,
        payload_dict: dict[str, Any],
    ) -> bool:
        if self._settings_callback is None:
            _logger.info(
                "No settings update callback configured; acknowledging message %s",
                message_id,
            )
            return True

        try:
            payload = CaptchaSettingsUpdatePayload.from_mapping(payload_dict)
        except CaptchaPayloadError as exc:
            _logger.warning(
                "Captcha settings update %s rejected: %s",
                message_id,
                exc,
            )
            return True

        try:
            result = self._settings_callback(payload)
            if inspect.isawaitable(result):
                await result
        except Exception:
            _logger.exception(
                "Failed to handle captcha settings update for guild %s",
                payload.guild_id,
            )

        return True

    async def handle_processing_error(  # type: ignore[override]
        self,
        message: RedisStreamMessage,
        exc: Exception,
    ) -> bool:
        _logger.exception(
            "Unexpected error while processing captcha callback %s/%s",
            message.stream,
            message.message_id,
            exc_info=exc,
        )
        return True

    async def after_message(self, message: RedisStreamMessage, *, acknowledged: bool) -> None:  # type: ignore[override]
        self._last_message_id = message.message_id

    async def _requeue(self, fields: dict[str, Any]) -> None:
        redis = self.redis
        if redis is None or self._config_ext.max_requeue_attempts <= 0:
            return

        attempts = self._coerce_int(fields.get("delivery_attempts")) or 0
        if attempts >= self._config_ext.max_requeue_attempts:
            _logger.info(
                "Dropping captcha callback after %s delivery attempts",
                attempts,
            )
            return

        new_fields = dict(fields)
        new_fields["delivery_attempts"] = str(attempts + 1)
        try:
            await redis.xadd(self._config.stream, new_fields)
            _logger.info(
                "Requeued captcha callback for guild %s",
                new_fields.get("guildId") or new_fields.get("guild_id"),
            )
        except Exception:
            _logger.exception("Failed to requeue captcha callback for other consumers")

    @property
    def last_message_id(self) -> str:
        return self._last_message_id

    # ------------------------------------------------------------------
    # Compatibility helpers for existing tests
    # ------------------------------------------------------------------
    async def _process_message(self, stream: str, message_id: str, fields: dict[str, Any]) -> bool:
        message = RedisStreamMessage(
            stream=stream,
            message_id=message_id,
            fields=normalize_stream_fields(fields),
        )
        return await self.handle_message(message)

    async def _claim_stale_messages(self) -> None:
        if self.redis is None:
            return
        await self.reclaim_pending(self.redis)

    # ------------------------------------------------------------------
    # Static helpers reused by tests and internal logic
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_xautoclaim_response(response: Any) -> tuple[str, Iterable[tuple[str, Any]]]:
        if isinstance(response, (list, tuple)):
            if len(response) < 2:
                raise ValueError("XAUTOCLAIM response must contain at least two elements")
            next_id = response[0]
            messages = response[1]
            return next_id, messages
        next_id: str | None = None
        for attr in ("next_start_id", "next_id", "next", "id"):
            if hasattr(response, attr):
                next_id = getattr(response, attr)
                break
        messages: Any | None = None
        for attr in ("messages", "entries", "ids"):
            if hasattr(response, attr):
                messages = getattr(response, attr)
                break
        if next_id is None or messages is None:
            raise TypeError(
                f"Cannot interpret XAUTOCLAIM response of type {type(response).__name__}",
            )
        return next_id, messages

    @staticmethod
    def _extract_payload_preview(fields: Any, *, max_length: int = 256) -> str | None:
        mapping = normalize_stream_fields(fields)
        payload = mapping.get("payload")
        if payload is None:
            return None
        text = CaptchaStreamListener._coerce_text(payload)
        if len(text) > max_length:
            return text[:max_length] + "..."
        return text

    @staticmethod
    def _coerce_int(value: Any) -> int | None:
        try:
            decoded = decode_if_bytes(value)
            return int(str(decoded))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _coerce_text(value: Any) -> str:
        decoded = decode_if_bytes(value)
        if isinstance(decoded, str):
            return decoded
        return str(decoded)
