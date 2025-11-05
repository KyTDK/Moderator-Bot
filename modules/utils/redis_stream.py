"""Shared helpers for Redis stream consumers."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Iterable, List, Mapping, MutableMapping, Sequence

try:  # pragma: no cover - optional dependency
    from redis.asyncio import Redis as RedisClient
    from redis.asyncio import from_url as redis_from_url
    from redis.exceptions import (
        ConnectionError as RedisConnectionError,
        ResponseError,
    )
except ModuleNotFoundError:  # pragma: no cover - handled gracefully by consumers
    RedisClient = None  # type: ignore[assignment]
    redis_from_url = None  # type: ignore[assignment]
    RedisConnectionError = Exception  # type: ignore[assignment]
    ResponseError = Exception  # type: ignore[assignment]

__all__ = [
    "RedisStreamConfig",
    "RedisStreamMessage",
    "RedisStreamConsumer",
    "decode_if_bytes",
    "normalize_stream_fields",
    "redis_from_url",
]


@dataclass(slots=True)
class RedisStreamConfig:
    """Configuration shared by Redis stream consumers."""

    enabled: bool
    redis_url: str | None
    stream: str
    group: str
    consumer_name: str
    block_ms: int = 10_000
    fetch_count: int = 20
    start_id: str = "0"
    max_concurrency: int = 1
    retry_initial_delay: float = 1.0
    retry_max_delay: float = 30.0


@dataclass(slots=True)
class RedisStreamMessage:
    """Normalized Redis stream entry."""

    stream: str
    message_id: str
    fields: dict[str, Any]


def decode_if_bytes(value: Any) -> Any:
    """Return ``value`` decoded to ``str`` when it's a bytes-like object."""

    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.decode("utf-8", "replace")
    return value


def normalize_stream_fields(raw_fields: Any) -> dict[str, Any]:
    """Convert raw Redis stream field payload to a ``dict[str, Any]``."""

    if isinstance(raw_fields, Mapping):
        return {
            str(decode_if_bytes(key)): decode_if_bytes(value)
            for key, value in raw_fields.items()
        }

    normalized: dict[str, Any] = {}
    if isinstance(raw_fields, Sequence):
        for item in raw_fields:
            if isinstance(item, Sequence) and len(item) == 2:
                key, value = item
                normalized[str(decode_if_bytes(key))] = decode_if_bytes(value)
    return normalized


class RedisStreamConsumer:
    """Generic Redis stream consumer with optional concurrency and retry support."""

    def __init__(
        self,
        config: RedisStreamConfig,
        *,
        logger: logging.Logger | None = None,
        delete_after_ack: bool = False,
        name: str | None = None,
        redis_factory: Callable[..., RedisClient] | None = None,
    ) -> None:
        self._config = config
        self._logger = logger or logging.getLogger(
            name or f"{__name__}.{self.__class__.__name__}"
        )
        self._delete_after_ack = delete_after_ack
        self._redis: RedisClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        self._max_concurrency = max(1, int(config.max_concurrency))
        self._worker_semaphore = asyncio.Semaphore(self._max_concurrency)
        self._inflight: set[asyncio.Task[None]] = set()
        self._retry_delay = config.retry_initial_delay
        self._redis_factory = redis_factory

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def redis(self) -> RedisClient | None:
        return self._redis

    async def start(self) -> bool:
        if not self._config.enabled:
            self._logger.debug("Stream consumer disabled; not starting.")
            return False
        factory = self._redis_factory or redis_from_url
        if factory is None or RedisClient is None:
            self._logger.warning(
                "redis-py is unavailable; stream consumer %s cannot start.",
                self._config.stream,
            )
            return False
        if self._config.redis_url is None:
            self._logger.warning(
                "Redis URL missing; stream consumer %s cannot start.",
                self._config.stream,
            )
            return False
        if self.is_running:
            return True

        redis = factory(self._config.redis_url, decode_responses=True)
        try:
            await self._ensure_consumer_group(redis)
        except (RedisConnectionError, OSError) as exc:
            await redis.close()
            await redis.connection_pool.disconnect()
            self._logger.error(
                "Unable to connect to Redis stream %s at %s: %s",
                self._config.stream,
                self._config.redis_url,
                exc,
            )
            return False
        except Exception:
            await redis.close()
            await redis.connection_pool.disconnect()
            raise

        self._redis = redis
        self._stopped.clear()
        try:
            task = asyncio.create_task(self._run_loop(), name=f"{self.__class__.__name__}-loop")
        except TypeError:  # pragma: no cover - Python <3.8 compatibility
            task = asyncio.create_task(self._run_loop())
        task.add_done_callback(lambda _: self._stopped.set())
        self._task = task
        self._logger.info(
            "Subscribed to Redis stream '%s' as consumer %s/%s",
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
            finally:
                self._task = None

        await self._wait_for_workers()

        if self._redis is not None:
            try:
                await self._redis.close()
            finally:
                await self._redis.connection_pool.disconnect()
                self._redis = None

    async def _ensure_consumer_group(self, redis: RedisClient) -> None:
        try:
            await redis.xgroup_create(
                self._config.stream,
                self._config.group,
                id=self._config.start_id,
                mkstream=True,
            )
            self._logger.info(
                "Created Redis consumer group %s on stream %s starting at %s",
                self._config.group,
                self._config.stream,
                self._config.start_id,
            )
        except ResponseError as exc:  # pragma: no branch - redis specific
            if "BUSYGROUP" not in str(exc):
                raise
            self._logger.debug(
                "Consumer group %s already exists on stream %s",
                self._config.group,
                self._config.stream,
            )

    async def _run_loop(self) -> None:
        assert self._redis is not None
        redis = self._redis
        retry_delay = self._config.retry_initial_delay
        max_delay = max(self._config.retry_initial_delay, self._config.retry_max_delay)

        while not self._stopped.is_set():
            try:
                await self.reclaim_pending(redis)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception("Failed to reclaim pending entries for %s", self._config.stream)

            try:
                entries = await redis.xreadgroup(
                    self._config.group,
                    self._config.consumer_name,
                    self.streams,
                    count=self._config.fetch_count,
                    block=self._config.block_ms,
                )
                retry_delay = self._config.retry_initial_delay
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                delay = retry_delay
                retry_delay = min(retry_delay * 2, max_delay)
                await self.on_poll_error(exc, delay)
                await asyncio.sleep(delay)
                continue

            if not entries:
                await self.on_idle()
                continue

            for message in self._iter_messages(entries):
                if self._max_concurrency > 1:
                    await self._worker_semaphore.acquire()
                    try:
                        task = asyncio.create_task(self._handle_single(message))
                    except TypeError:  # pragma: no cover - compatibility shim
                        task = asyncio.create_task(self._handle_single(message))
                    self._inflight.add(task)
                    task.add_done_callback(self._on_worker_done)
                else:
                    await self._handle_single(message)

    async def _handle_single(self, message: RedisStreamMessage) -> None:
        try:
            should_ack = await self.handle_message(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            should_ack = await self.handle_processing_error(message, exc)
        else:
            should_ack = bool(should_ack)
        finally:
            if self._max_concurrency > 1:
                self._worker_semaphore.release()

        if should_ack:
            await self._acknowledge(message)
        await self.after_message(message, acknowledged=should_ack)

    async def _acknowledge(self, message: RedisStreamMessage) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.xack(
                message.stream,
                self._config.group,
                message.message_id,
            )
            if self._delete_after_ack:
                await self._redis.xdel(message.stream, message.message_id)
        except Exception:
            self._logger.exception(
                "Failed to acknowledge Redis stream entry %s/%s",
                message.stream,
                message.message_id,
            )

    async def reclaim_pending(self, redis: RedisClient) -> None:
        """Hook for subclasses that need to recover pending entries."""

    async def handle_processing_error(
        self,
        message: RedisStreamMessage,
        exc: Exception,
    ) -> bool:
        """Handle exceptions raised by ``handle_message``.

        Return ``True`` to acknowledge the message, ``False`` to leave it pending.
        """

        self._logger.exception(
            "Unhandled error while processing Redis stream entry %s/%s",
            message.stream,
            message.message_id,
            exc_info=exc,
        )
        return True

    async def after_message(
        self,
        message: RedisStreamMessage,
        *,
        acknowledged: bool,
    ) -> None:
        """Hook invoked after acknowledgement (or when ack is skipped)."""

    async def on_idle(self) -> None:
        """Hook invoked when a poll returns no entries."""

    async def on_poll_error(self, exc: Exception, delay: float) -> None:
        """Hook invoked when polling fails. Default behavior logs a warning."""

        self._logger.warning(
            "Redis stream poll error for %s; retrying in %.1fs (%s)",
            self._config.stream,
            delay,
            exc,
        )

    @property
    def streams(self) -> Mapping[str, str]:
        return {self._config.stream: ">"}

    def _iter_messages(
        self,
        entries: Sequence[tuple[str, List[tuple[str, MutableMapping[str, Any]]]]],
    ) -> Iterable[RedisStreamMessage]:
        for stream_name, messages in entries:
            for message_id, fields in messages:
                normalized = normalize_stream_fields(fields)
                yield RedisStreamMessage(stream=stream_name, message_id=message_id, fields=normalized)

    def _on_worker_done(self, task: asyncio.Task[None]) -> None:
        self._inflight.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            self._logger.error("Redis stream worker raised unexpected error", exc_info=exc)

    async def _wait_for_workers(self) -> None:
        if not self._inflight:
            return
        pending = [task for task in self._inflight if not task.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._inflight.clear()

    async def handle_message(self, message: RedisStreamMessage) -> bool:
        """Process a single stream message. Must be implemented by subclasses."""

        raise NotImplementedError
