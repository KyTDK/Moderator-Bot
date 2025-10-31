from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True, slots=True)
class QueueConfig:
    max_workers: int
    autoscale_max: int


@dataclass(frozen=True, slots=True)
class AutoscaleConfig:
    free_backlog_high: int
    accelerated_backlog_high: int
    backlog_low: int
    check_interval: float
    scale_down_grace: float


@dataclass(frozen=True, slots=True)
class MonitorConfig:
    check_interval: float = 15.0
    required_hits: int = 3
    cooldown: float = 300.0


@dataclass(frozen=True, slots=True)
class AggregatedModerationConfig:
    free: QueueConfig
    accelerated: QueueConfig
    autoscale: AutoscaleConfig
    monitor: MonitorConfig


# Environment variable names used to configure the aggregated moderation workers.
# Baseline worker pool size for the free queue.
ENV_FREE_MAX_WORKERS: Final[str] = "FREE_MAX_WORKERS"
# Baseline worker pool size for the accelerated queue.
ENV_ACCELERATED_MAX_WORKERS: Final[str] = "ACCELERATED_MAX_WORKERS"
# Upper autoscale limit for the free queue.
ENV_FREE_MAX_WORKERS_BURST: Final[str] = "FREE_MAX_WORKERS_BURST"
# Upper autoscale limit for the accelerated queue.
ENV_ACCELERATED_MAX_WORKERS_BURST: Final[str] = "ACCELERATED_MAX_WORKERS_BURST"
# Backlog that triggers autoscale for the free queue.
ENV_FREE_WORKER_BACKLOG_HIGH: Final[str] = "FREE_WORKER_BACKLOG_HIGH"
# Backlog that triggers autoscale for the accelerated queue.
ENV_ACCELERATED_WORKER_BACKLOG_HIGH: Final[str] = "ACCELERATED_WORKER_BACKLOG_HIGH"
# Legacy shared backlog threshold (used as a fallback).
ENV_WORKER_BACKLOG_HIGH: Final[str] = "WORKER_BACKLOG_HIGH"
# Backlog level where autoscale can scale down.
ENV_WORKER_BACKLOG_LOW: Final[str] = "WORKER_BACKLOG_LOW"
# Polling cadence for autoscale decisions.
ENV_AUTOSCALE_CHECK_INTERVAL: Final[str] = "WORKER_AUTOSCALE_CHECK_INTERVAL"
# Cooldown before reducing workers after a burst.
ENV_AUTOSCALE_SCALE_DOWN_GRACE: Final[str] = "WORKER_AUTOSCALE_SCALE_DOWN_GRACE"


# Default values for the above environment variables to keep configuration self-contained.
ENV_DEFAULTS: Final = {
    ENV_FREE_MAX_WORKERS: "2",
    ENV_ACCELERATED_MAX_WORKERS: "5",
    ENV_FREE_MAX_WORKERS_BURST: "5",
    ENV_ACCELERATED_MAX_WORKERS_BURST: "10",
    ENV_FREE_WORKER_BACKLOG_HIGH: "100",
    ENV_ACCELERATED_WORKER_BACKLOG_HIGH: "30",
    ENV_WORKER_BACKLOG_HIGH: "30",
    ENV_WORKER_BACKLOG_LOW: "5",
    ENV_AUTOSCALE_CHECK_INTERVAL: "2",
    ENV_AUTOSCALE_SCALE_DOWN_GRACE: "15",
}


def _int_env(name: str, default: str | None) -> int:
    """Fetch an integer from the environment, falling back to the provided default."""
    raw = os.getenv(name, default or "")
    try:
        return int(raw)
    except (TypeError, ValueError):
        if default is None:
            raise
        return int(default)


def _float_env(name: str, default: str) -> float:
    """Fetch a float from the environment, falling back to the provided default."""
    raw = os.getenv(name, default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def load_config() -> AggregatedModerationConfig:
    free_max = _int_env(ENV_FREE_MAX_WORKERS, ENV_DEFAULTS[ENV_FREE_MAX_WORKERS])
    accel_max = _int_env(ENV_ACCELERATED_MAX_WORKERS, ENV_DEFAULTS[ENV_ACCELERATED_MAX_WORKERS])

    # Allow bursts to fall back to the steady-state worker count when no override is supplied.
    free_burst_default = ENV_DEFAULTS[ENV_FREE_MAX_WORKERS_BURST] or str(free_max)
    accel_burst_default = ENV_DEFAULTS[ENV_ACCELERATED_MAX_WORKERS_BURST] or str(accel_max)
    free_burst = _int_env(ENV_FREE_MAX_WORKERS_BURST, free_burst_default)
    accel_burst = _int_env(ENV_ACCELERATED_MAX_WORKERS_BURST, accel_burst_default)

    shared_backlog_high = os.getenv(ENV_WORKER_BACKLOG_HIGH)
    # Prefer queue-specific backlog thresholds, but honour the shared knob when set.
    free_backlog_high_default = shared_backlog_high or ENV_DEFAULTS[ENV_FREE_WORKER_BACKLOG_HIGH]
    accel_backlog_high_default = shared_backlog_high or ENV_DEFAULTS[ENV_ACCELERATED_WORKER_BACKLOG_HIGH]
    free_backlog_high = _int_env(ENV_FREE_WORKER_BACKLOG_HIGH, free_backlog_high_default)
    accel_backlog_high = _int_env(
        ENV_ACCELERATED_WORKER_BACKLOG_HIGH,
        accel_backlog_high_default,
    )

    backlog_low = _int_env(ENV_WORKER_BACKLOG_LOW, ENV_DEFAULTS[ENV_WORKER_BACKLOG_LOW])
    check_interval = _float_env(
        ENV_AUTOSCALE_CHECK_INTERVAL,
        ENV_DEFAULTS[ENV_AUTOSCALE_CHECK_INTERVAL],
    )
    scale_down_grace = _float_env(
        ENV_AUTOSCALE_SCALE_DOWN_GRACE,
        ENV_DEFAULTS[ENV_AUTOSCALE_SCALE_DOWN_GRACE],
    )

    return AggregatedModerationConfig(
        free=QueueConfig(max_workers=free_max, autoscale_max=free_burst),
        accelerated=QueueConfig(max_workers=accel_max, autoscale_max=accel_burst),
        autoscale=AutoscaleConfig(
            free_backlog_high=free_backlog_high,
            accelerated_backlog_high=accel_backlog_high,
            backlog_low=backlog_low,
            check_interval=check_interval,
            scale_down_grace=scale_down_grace,
        ),
        monitor=MonitorConfig(),
    )


__all__ = [
    "AggregatedModerationConfig",
    "AutoscaleConfig",
    "MonitorConfig",
    "QueueConfig",
    "load_config",
]
