from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from discord.ext import commands

try:  # pragma: no cover - exercised indirectly via start()
    from redis.asyncio import from_url as redis_from_url
    from redis.exceptions import (
        ConnectionError as RedisConnectionError,
        ResponseError,
    )
except ModuleNotFoundError:  # pragma: no cover - handled gracefully in start()
    redis_from_url = None  # type: ignore[assignment]
    ResponseError = Exception  # type: ignore[assignment]
    RedisConnectionError = Exception  # type: ignore[assignment]

if TYPE_CHECKING:
    from redis.asyncio import Redis as RedisClient
else:  # pragma: no cover - used when redis-py is not installed
    RedisClient = Any

from .config import CaptchaStreamConfig
from .models import (
    CaptchaCallbackPayload,
    CaptchaPayloadError,
    CaptchaProcessingError,
    CaptchaSettingsUpdatePayload,
)
from .processor import CaptchaCallbackProcessor
from .sessions import CaptchaSessionStore

_logger = logging.getLogger(__name__)

def _ensure_logger_configured() -> None:
    """Attach a fallback handler so INFO logs surface when logging is uninitialised."""
    root_logger = logging.getLogger()
    if root_logger.handlers and root_logger.isEnabledFor(logging.INFO):
        return
    if _logger.handlers:
        return

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s"))
    handler.setLevel(logging.INFO)
    _logger.addHandler(handler)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False

SettingsUpdateCallback = Callable[[CaptchaSettingsUpdatePayload], Awaitable[None] | None]

class CaptchaStreamListener:
    """Consumes captcha callbacks from a Redis stream using a consumer group."""

    def __init__(
        self,
        bot: commands.Bot,
        config: CaptchaStreamConfig,
        session_store: CaptchaSessionStore,
        settings_update_callback: SettingsUpdateCallback | None = None,
    ) -> None:
        self._bot = bot
        self._config = config
        self._processor = CaptchaCallbackProcessor(bot, session_store)
        self._redis: RedisClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        self._settings_callback: SettingsUpdateCallback | None = settings_update_callback

    async def start(self) -> bool:
        _ensure_logger_configured()
        if not self._config.enabled:
            return False

        if self._redis is not None:
            return True

        if redis_from_url is None:
            _logger.warning("redis-py is not installed; captcha stream listener cannot start")
            return False

        if not self._config.redis_url:
            _logger.warning("Captcha stream listener enabled but no Redis URL configured; skipping startup")
            return False

        self._redis = redis_from_url(self._config.redis_url, decode_responses=True)

        try:
            await self._ensure_consumer_group()
        except RedisConnectionError as exc:
            _logger.error(
                "Unable to connect to Redis for captcha callbacks at %s: %s. Captcha callbacks will be disabled.",
                self._config.redis_url,
                exc,
            )
            await self._close_redis()
            return False
        except OSError as exc:
            _logger.error(
                "Unexpected OS error while connecting to Redis for captcha callbacks at %s: %s. Captcha callbacks will be disabled.",
                self._config.redis_url,
                exc,
            )
            await self._close_redis()
            return False
        except Exception:
            await self._close_redis()
            raise

        self._stopped.clear()
        self._task = asyncio.create_task(self._run_loop(), name="captcha-stream-listener")
        _logger.info(
            "Subscribed to captcha callback stream %s as consumer %s/%s",
            self._config.stream,
            self._config.group,
            self._config.consumer_name,
        )
        return True

    async def stop(self) -> None:
        self._stopped.set()

        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        await self._close_redis()

    async def _close_redis(self) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.close()
        finally:
            await self._redis.connection_pool.disconnect()
            self._redis = None

    async def _ensure_consumer_group(self) -> None:
        assert self._redis is not None
        try:
            await self._redis.xgroup_create(
                self._config.stream,
                self._config.group,
                id="$",
                mkstream=True,
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
            _logger.debug(
                "Captcha consumer group %s already exists for stream %s",
                self._config.group,
                self._config.stream,
            )

    async def _run_loop(self) -> None:
        assert self._redis is not None
        streams = {self._config.stream: ">"}
        block = self._config.block_ms
        while not self._stopped.is_set():
            try:
                entries = await self._redis.xreadgroup(
                    self._config.group,
                    self._config.consumer_name,
                    streams,
                    count=self._config.batch_size,
                    block=block,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("Error while reading captcha callback stream; retrying in 5 seconds")
                await asyncio.sleep(5)
                continue

            if not entries:
                continue

            for stream_name, messages in entries:
                for message_id, fields in messages:
                    await self._handle_message(stream_name, message_id, fields)

    async def _handle_message(self, stream: str, message_id: str, fields: Any) -> None:
        should_ack = True
        try:
            should_ack = await self._process_message(stream, message_id, fields)
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception(
                "Unexpected error while processing captcha callback %s from stream %s",
                message_id,
                stream,
            )
        finally:
            if should_ack:
                await self._acknowledge(message_id)

    async def _process_message(self, stream: str, message_id: str, raw_fields: Any) -> bool:
        if self._redis is None:
            return True

        _logger.info(
            "[captcha/callback] received redis entry %s/%s: raw_fields=%r",
            stream,
            message_id,
            raw_fields,
        )

        fields = self._coerce_field_mapping(raw_fields)
        _logger.info(
            "[captcha/callback] normalized redis entry %s/%s: fields=%r",
            stream,
            message_id,
            fields,
        )
        payload_raw = fields.get("payload")
        if not payload_raw:
            _logger.warning("Captcha callback %s missing payload field; acknowledging", message_id)
            return True

        payload_text = self._coerce_text(payload_raw)
        _logger.info(
            "[captcha/callback] payload text for %s/%s: %s",
            stream,
            message_id,
            payload_text,
        )

        if self._config.shared_secret:
            signature = fields.get("signature")
            signature_text = (
                self._coerce_text(signature) if signature is not None else ""
            )
            expected = hmac.new(
                self._config.shared_secret,
                payload_text.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            if not signature_text or not hmac.compare_digest(signature_text.lower(), expected.lower()):
                _logger.warning(
                    "Captcha callback %s has invalid signature; ignoring message",
                    message_id,
                )
                return True

        try:
            payload_dict = json.loads(payload_text)
        except json.JSONDecodeError:
            _logger.warning("Captcha callback %s contains invalid JSON payload", message_id)
            return True

        event_type_raw = payload_dict.get("eventType") or payload_dict.get("event_type")
        event_type = str(event_type_raw).strip() if isinstance(event_type_raw, str) else ""

        if event_type == "captcha.settings.updated":
            return await self._handle_settings_update_event(message_id, payload_dict)

        if event_type and event_type not in {
            "captcha.verification.completed",
            "captcha.verification.failed",
        }:
            _logger.debug("Ignoring unknown captcha event type %s", event_type)
            return True

        return await self._handle_verification_event(message_id, payload_dict, fields)

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
            _logger.debug(
                "Skipping captcha callback %s for guild %s; not managed by this instance",
                message_id,
                payload.guild_id,
            )
            await self._requeue(fields)
            return True

        try:
            await self._processor.process(payload)
            _logger.debug(
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
            _logger.debug(
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

    async def _acknowledge(self, message_id: str) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.xack(self._config.stream, self._config.group, message_id)
        except Exception:
            _logger.exception("Failed to acknowledge captcha callback %s", message_id)

    async def _requeue(self, fields: dict[str, Any]) -> None:
        if self._redis is None:
            return
        if self._config.max_requeue_attempts <= 0:
            return

        attempts = self._coerce_int(fields.get("delivery_attempts"))
        if attempts is None:
            attempts = 0

        if attempts >= self._config.max_requeue_attempts:
            _logger.info(
                "Dropping captcha callback after %s delivery attempts", attempts,
            )
            return

        new_fields = dict(fields)
        new_fields["delivery_attempts"] = str(attempts + 1)
        try:
            await self._redis.xadd(self._config.stream, new_fields)
            _logger.debug("Requeued captcha callback for guild %s", new_fields.get("guildId") or new_fields.get("guild_id"))
        except Exception:
            _logger.exception("Failed to requeue captcha callback for other consumers")

    @staticmethod
    def _coerce_field_mapping(raw_fields: Any) -> dict[str, Any]:
        if isinstance(raw_fields, dict):
            return {
                CaptchaStreamListener._coerce_key(key): CaptchaStreamListener._decode_if_bytes(value)
                for key, value in raw_fields.items()
            }
        # redis-py returns list of tuples when decode_responses is False; guard defensively.
        mapping: dict[str, Any] = {}
        if isinstance(raw_fields, (list, tuple)):
            for item in raw_fields:
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    key, value = item
                    mapping[CaptchaStreamListener._coerce_key(key)] = CaptchaStreamListener._decode_if_bytes(value)
        return mapping

    @staticmethod
    def _coerce_int(value: Any) -> int | None:
        try:
            decoded = CaptchaStreamListener._decode_if_bytes(value)
            return int(str(decoded))
        except (TypeError, ValueError):
            return None
        
    @staticmethod
    def _decode_if_bytes(value: Any) -> Any:
        if isinstance(value, (bytes, bytearray)):
            try:
                return value.decode("utf-8")
            except UnicodeDecodeError:
                return value.decode("utf-8", "replace")
        return value

    @staticmethod
    def _coerce_key(value: Any) -> str:
        decoded = CaptchaStreamListener._decode_if_bytes(value)
        return str(decoded)

    @staticmethod
    def _coerce_text(value: Any) -> str:
        decoded = CaptchaStreamListener._decode_if_bytes(value)
        if isinstance(decoded, str):
            return decoded
        return str(decoded)
