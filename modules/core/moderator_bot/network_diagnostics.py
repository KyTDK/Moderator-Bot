from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import socket
import time
from typing import Awaitable, Callable, Iterable, Sequence

log = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class ProbeTarget:
    host: str
    port: int
    label: str


@dataclass(slots=True, frozen=True)
class NetworkDiagnosticAlert:
    errors: tuple[str, ...]
    consecutive_failures: int
    failure_duration: float
    last_success_age: float | None
    last_success_latency_ms: float | None


class NetworkDiagnosticsTask:
    """Periodically probe DNS lookups for key hosts and raise alerts when they fail."""

    def __init__(
        self,
        *,
        interval_seconds: float = 45.0,
        failure_threshold: int = 3,
        alert_cooldown_seconds: float = 900.0,
        targets: Sequence[ProbeTarget] | None = None,
        alert_callback: Callable[[NetworkDiagnosticAlert], Awaitable[None]] | None = None,
    ) -> None:
        self._interval_seconds = max(5.0, interval_seconds)
        self._failure_threshold = max(1, failure_threshold)
        self._alert_cooldown_seconds = max(30.0, alert_cooldown_seconds)
        self._targets: tuple[ProbeTarget, ...] = tuple(
            targets
            if targets
            else (
                ProbeTarget(host="gateway.discord.gg", port=443, label="discord-gateway"),
                ProbeTarget(host="discord.com", port=443, label="discord-api"),
                ProbeTarget(host="google.com", port=443, label="google"),
            )
        )
        self._alert_callback = alert_callback
        self._task: asyncio.Task[None] | None = None
        self._consecutive_failures = 0
        self._first_failure_monotonic: float | None = None
        self._last_success_monotonic: float | None = None
        self._last_success_latency_ms: float | None = None
        self._last_alert_monotonic: float = 0.0
        self._stop_event = asyncio.Event()

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._runner(), name="network-diagnostics")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    async def _runner(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._probe_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Network diagnostics probe failed unexpectedly")
                await asyncio.sleep(5)
                continue

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._interval_seconds
                )
            except asyncio.TimeoutError:
                continue

    async def _probe_once(self) -> None:
        loop = asyncio.get_running_loop()
        errors: list[str] = []
        latency_samples: list[float] = []

        for target in self._targets:
            try:
                latency_ms = await self._resolve(loop, target)
                latency_samples.append(latency_ms)
            except (socket.gaierror, OSError) as exc:
                errors.append(f"{target.label}: {exc.__class__.__name__}: {exc}")
            except Exception as exc:
                log.debug("Probe failed for %s: %s", target.label, exc, exc_info=True)
                errors.append(f"{target.label}: {exc.__class__.__name__}")

        if errors:
            self._handle_failure(errors)
        else:
            self._handle_success(latency_samples)

    async def _resolve(self, loop: asyncio.AbstractEventLoop, target: ProbeTarget) -> float:
        start = time.perf_counter()
        await loop.getaddrinfo(target.host, target.port, type=socket.SOCK_STREAM)
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        return elapsed_ms

    def _handle_success(self, latency_samples: Iterable[float]) -> None:
        self._consecutive_failures = 0
        self._first_failure_monotonic = None
        self._last_success_monotonic = time.monotonic()
        try:
            self._last_success_latency_ms = max(latency_samples)
        except ValueError:
            self._last_success_latency_ms = None

    def _handle_failure(self, errors: list[str]) -> None:
        now = time.monotonic()
        if self._consecutive_failures == 0:
            self._first_failure_monotonic = now
        self._consecutive_failures += 1
        failure_duration = (
            now - self._first_failure_monotonic
            if self._first_failure_monotonic is not None
            else 0.0
        )

        if self._consecutive_failures < self._failure_threshold:
            return

        if now - self._last_alert_monotonic < self._alert_cooldown_seconds:
            return

        self._last_alert_monotonic = now
        last_success_age = (
            now - self._last_success_monotonic
            if self._last_success_monotonic is not None
            else None
        )
        alert = NetworkDiagnosticAlert(
            errors=tuple(errors),
            consecutive_failures=self._consecutive_failures,
            failure_duration=failure_duration,
            last_success_age=last_success_age,
            last_success_latency_ms=self._last_success_latency_ms,
        )

        if self._alert_callback is not None:
            asyncio.create_task(self._safe_call(alert))

    async def _safe_call(self, alert: NetworkDiagnosticAlert) -> None:
        try:
            await self._alert_callback(alert)
        except Exception:
            log.exception("Network diagnostic alert callback failed")


__all__ = ["NetworkDiagnosticAlert", "NetworkDiagnosticsTask", "ProbeTarget"]
