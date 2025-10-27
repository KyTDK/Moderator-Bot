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
    adaptive_limits: bool = False


@dataclass(frozen=True, slots=True)
class AutoscaleConfig:
    backlog_high: int
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


ENV_DEFAULTS: Final = {
    "FREE_MAX_WORKERS": "2",
    "ACCELERATED_MAX_WORKERS": "5",
    "FREE_MAX_WORKERS_BURST": None,
    "ACCELERATED_MAX_WORKERS_BURST": None,
    "FREE_ADAPTIVE_LIMITS": "false",
    "ACCELERATED_ADAPTIVE_LIMITS": "false",
    "WORKER_BACKLOG_HIGH": "30",
    "WORKER_BACKLOG_LOW": "5",
    "WORKER_AUTOSCALE_CHECK_INTERVAL": "2",
    "WORKER_AUTOSCALE_SCALE_DOWN_GRACE": "15",
}


def _int_env(name: str, default: str | None) -> int:
    raw = os.getenv(name, default or "")
    try:
        return int(raw)
    except (TypeError, ValueError):
        if default is None:
            raise
        return int(default)


def _float_env(name: str, default: str) -> float:
    raw = os.getenv(name, default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def _bool_env(name: str, default: str) -> bool:
    raw = os.getenv(name, default)
    if raw is None:
        return default.lower() in {"1", "true", "yes", "on"}
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def load_config() -> AggregatedModerationConfig:
    free_max = _int_env("FREE_MAX_WORKERS", ENV_DEFAULTS["FREE_MAX_WORKERS"])
    accel_max = _int_env("ACCELERATED_MAX_WORKERS", ENV_DEFAULTS["ACCELERATED_MAX_WORKERS"])
    free_burst = _int_env("FREE_MAX_WORKERS_BURST", str(free_max))
    accel_burst = _int_env("ACCELERATED_MAX_WORKERS_BURST", str(accel_max))
    free_adaptive = _bool_env("FREE_ADAPTIVE_LIMITS", ENV_DEFAULTS["FREE_ADAPTIVE_LIMITS"])
    accel_adaptive = _bool_env("ACCELERATED_ADAPTIVE_LIMITS", ENV_DEFAULTS["ACCELERATED_ADAPTIVE_LIMITS"])

    backlog_high = _int_env("WORKER_BACKLOG_HIGH", ENV_DEFAULTS["WORKER_BACKLOG_HIGH"])
    backlog_low = _int_env("WORKER_BACKLOG_LOW", ENV_DEFAULTS["WORKER_BACKLOG_LOW"])
    check_interval = _float_env("WORKER_AUTOSCALE_CHECK_INTERVAL", ENV_DEFAULTS["WORKER_AUTOSCALE_CHECK_INTERVAL"])
    scale_down_grace = _float_env("WORKER_AUTOSCALE_SCALE_DOWN_GRACE", ENV_DEFAULTS["WORKER_AUTOSCALE_SCALE_DOWN_GRACE"])

    return AggregatedModerationConfig(
        free=QueueConfig(max_workers=free_max, autoscale_max=free_burst, adaptive_limits=free_adaptive),
        accelerated=QueueConfig(max_workers=accel_max, autoscale_max=accel_burst, adaptive_limits=accel_adaptive),
        autoscale=AutoscaleConfig(
            backlog_high=backlog_high,
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
