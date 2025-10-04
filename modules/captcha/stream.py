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
        self._supports_xautoclaim = True
        self._autoclaim_warning_emitted = False
        self._last_message_id: str = config.start_id
        self._max_concurrency = max(1, config.max_concurrency)
        self._worker_semaphore = asyncio.Semaphore(self._max_concurrency)
        self._inflight_tasks: set[asyncio.Task[None]] = set()

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
        self._worker_semaphore = asyncio.Semaphore(self._max_concurrency)
        self._inflight_tasks.clear()
        self._task = asyncio.create_task(self._run_loop(), name="captcha-stream-listener")
        _logger.info(
            "Subscribed to captcha callback stream %s as consumer %s/%s (start=%s)",
            self._config.stream,
            self._config.group,
            self._config.consumer_name,
            self._config.start_id,
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

        await self._wait_for_workers()
        await self._close_redis()

    async def _wait_for_workers(self) -> None:
        if not self._inflight_tasks:
            return

        pending = [task for task in self._inflight_tasks if not task.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        self._inflight_tasks.clear()

    async def _close_redis(self) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.close()
        finally:
            await self._redis.connection_pool.disconnect()
            self._redis = None

    @property
    def last_message_id(self) -> str:
        """Return the last Redis stream ID processed by this listener."""
        return self._last_message_id

    async def _ensure_consumer_group(self) -> None:
        assert self._redis is not None
        try:
            await self._redis.xgroup_create(
                self._config.stream,
                self._config.group,
                id=self._config.start_id,
                mkstream=True,
            )
            _logger.info(
                "Ensured captcha consumer group %s on %s starting at %s",
                self._config.group,
                self._config.stream,
                self._config.start_id,
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
            _logger.info(
                "Captcha consumer group %s already exists for stream %s",
                self._config.group,
                self._config.stream,
            )

    async def _claim_stale_messages(self) -> None:
        redis = self._redis
        if redis is None:
            return
        idle_ms = self._config.pending_auto_claim_ms
        if idle_ms <= 0:
            return

        if self._supports_xautoclaim:
            try:
                await self._claim_with_xautoclaim(redis, idle_ms)
                return
            except ResponseError as exc:
                message = str(exc).lower()
                if "unknown command" in message or "syntax error" in message:
                    self._supports_xautoclaim = False
                    if not self._autoclaim_warning_emitted:
                        self._autoclaim_warning_emitted = True
                        _logger.info(
                            "Redis server does not support XAUTOCLAIM; falling back to XCLAIM for pending captcha callbacks.",
                        )
                else:
                    _logger.exception(
                        "Failed to auto-claim pending captcha callbacks using XAUTOCLAIM.",
                    )
                    return
            except Exception:
                _logger.exception(
                    "Failed to auto-claim pending captcha callbacks using XAUTOCLAIM.",
                )
                return

        await self._claim_with_xclaim(redis, idle_ms)

    async def _claim_with_xautoclaim(self, redis: RedisClient, idle_ms: int) -> None:
        stream = self._config.stream
        next_id = "0-0"
        while not self._stopped.is_set():
            response = await redis.xautoclaim(
                stream,
                self._config.group,
                self._config.consumer_name,
                idle_ms,
                next_id,
                count=self._config.batch_size,
            )
            next_id, messages = self._normalize_xautoclaim_response(response)
            if not messages:
                break
            for message_id, fields in messages:
                await self._handle_message(stream, message_id, fields)
            if not next_id or next_id == "0-0":
                break

    async def _claim_with_xclaim(self, redis: RedisClient, idle_ms: int) -> None:
        try:
            pending_entries = await redis.xpending_range(
                self._config.stream,
                self._config.group,
                min='-',
                max='+',
                count=self._config.batch_size,
            )
        except ResponseError:
            _logger.exception(
                "Failed to inspect pending captcha callbacks for stream %s",
                self._config.stream,
            )
            return
        except Exception:
            _logger.exception(
                "Failed to inspect pending captcha callbacks for stream %s",
                self._config.stream,
            )
            return

        if not pending_entries:
            return

        for entry in pending_entries:
            message_id = getattr(entry, 'message_id', None) or getattr(entry, 'id', None)
            idle_time = getattr(entry, 'idle', None) or getattr(entry, 'idle_time', None)
            if message_id is None or idle_time is None or idle_time < idle_ms:
                continue
            try:
                claimed = await redis.xclaim(
                    self._config.stream,
                    self._config.group,
                    self._config.consumer_name,
                    idle_ms,
                    [message_id],
                )
            except ResponseError:
                _logger.exception(
                    "Failed to claim pending captcha callback %s from stream %s",
                    message_id,
                    self._config.stream,
                )
                continue
            except Exception:
                _logger.exception(
                    "Failed to claim pending captcha callback %s from stream %s",
                    message_id,
                    self._config.stream,
                )
                continue

            for claimed_id, fields in claimed:
                await self._handle_message(self._config.stream, claimed_id, fields)


    async def _run_loop(self) -> None:
        assert self._redis is not None
        streams = {self._config.stream: ">"}
        block = self._config.block_ms
        retry_delay = 1.0
        max_retry = 30.0
        while not self._stopped.is_set():
            try:
                await self._claim_stale_messages()
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception(
                    "Failed to reclaim pending captcha callbacks for stream %s; continuing",
                    self._config.stream,
                )
            try:
                entries = await self._redis.xreadgroup(
                    self._config.group,
                    self._config.consumer_name,
                    streams,
                    count=self._config.batch_size,
                    block=block,
                )
                retry_delay = 1.0
            except asyncio.CancelledError:
                raise
            except (RedisConnectionError, OSError) as exc:
                delay = retry_delay
                retry_delay = min(retry_delay * 2, max_retry)
                _logger.warning(
                    "Transient Redis error while reading captcha callbacks; retrying in %.1fs (%s)",
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)
                continue
            except Exception:
                retry_delay = min(retry_delay * 2, max_retry)
                _logger.exception(
                    "Error while reading captcha callback stream; retrying in %.1fs",
                    retry_delay,
                )
                await asyncio.sleep(retry_delay)
                continue

            if not entries:
                continue

            batch_entries: list[tuple[str, str, Any]] = []
            batch_summary: list[dict[str, Any]] = []
            for stream_name, messages in entries:
                for message_id, fields in messages:
                    batch_entries.append((stream_name, message_id, fields))
                    payload_preview = self._extract_payload_preview(fields)
                    batch_summary.append(
                        {
                            "stream": stream_name,
                            "id": message_id,
                            "payload": payload_preview,
                        }
                    )

            if batch_summary:
                _logger.info(
                    "[captcha/callback] xreadgroup batch size=%s summary=%s",
                    len(batch_summary),
                    batch_summary,
                )

            for stream_name, message_id, fields in batch_entries:
                try:
                    await self._dispatch_message(stream_name, message_id, fields)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _logger.exception(
                        "Failed to dispatch captcha callback %s from stream %s",
                        message_id,
                        stream_name,
                    )

    async def _dispatch_message(self, stream: str, message_id: str, fields: Any) -> None:
        await self._worker_semaphore.acquire()
        try:
            task_coro = self._process_dispatched_message(stream, message_id, fields)
            task_name = f"captcha-callback:{message_id}"
            try:
                task = asyncio.create_task(task_coro, name=task_name)
            except TypeError:
                task = asyncio.create_task(task_coro)
        except Exception:
            self._worker_semaphore.release()
            raise

        self._inflight_tasks.add(task)
        task.add_done_callback(self._on_worker_done)

    async def _process_dispatched_message(self, stream: str, message_id: str, fields: Any) -> None:
        try:
            await self._handle_message(stream, message_id, fields)
        finally:
            self._worker_semaphore.release()

    def _on_worker_done(self, task: asyncio.Task[None]) -> None:
        self._inflight_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _logger.error(
                "Captcha callback worker raised unexpected error",
                exc_info=exc,
            )

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
            try:
                if should_ack:
                    await self._acknowledge(message_id)
            finally:
                self._last_message_id = message_id

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
            _logger.info("Ignoring unknown captcha event type %s", event_type)
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
            _logger.info(
                "Skipping captcha callback %s for guild %s; not managed by this instance",
                message_id,
                payload.guild_id,
            )
            await self._requeue(fields)
            return True

        try:
            await self._processor.process(payload)
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
            _logger.info("Requeued captcha callback for guild %s", new_fields.get("guildId") or new_fields.get("guild_id"))
        except Exception:
            _logger.exception("Failed to requeue captcha callback for other consumers")

    @staticmethod
    def _extract_payload_preview(fields: Any, *, max_length: int = 256) -> str | None:
        mapping = CaptchaStreamListener._coerce_field_mapping(fields)
        payload = mapping.get("payload")
        if payload is None:
            return None
        text = CaptchaStreamListener._coerce_text(payload)
        if len(text) > max_length:
            return text[:max_length] + "..."
        return text


    @staticmethod
    def _normalize_xautoclaim_response(response: Any) -> tuple[str, Any]:
        if isinstance(response, (list, tuple)):
            if len(response) < 2:
                raise ValueError("XAUTOCLAIM response must contain at least two elements")
            return response[0], response[1]
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

