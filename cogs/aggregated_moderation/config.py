from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AdaptiveQueuePolicy:
    name: str
    min_workers: int
    max_workers: int
    backlog_target: int
    backlog_low: int
    backlog_soft_limit: int
    catchup_batch: int
    provision_bias: float
    recovery_bias: float
    wait_threshold: float
    min_runtime: float
    maintain_backlog: bool


@dataclass(frozen=True, slots=True)
class AdaptiveControllerConfig:
    tick_interval: float = 2.0
    rate_window: float = 180.0
    scale_down_cooldown: float = 20.0


@dataclass(frozen=True, slots=True)
class MonitorConfig:
    check_interval: float = 15.0
    required_hits: int = 3
    cooldown: float = 300.0


@dataclass(frozen=True, slots=True)
class PerformanceMonitorConfig:
    check_interval: float = 30.0
    ratio_threshold: float = 0.9
    wait_ratio_threshold: float = 0.85
    min_runtime_seconds: float = 0.2
    min_completed_tasks: int = 50
    required_hits: int = 3
    cooldown: float = 900.0


@dataclass(frozen=True, slots=True)
class AggregatedModerationConfig:
    free_policy: AdaptiveQueuePolicy
    accelerated_policy: AdaptiveQueuePolicy
    controller: AdaptiveControllerConfig
    monitor: MonitorConfig
    performance_monitor: PerformanceMonitorConfig


def _clamp_int(value: int, *, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


def load_config() -> AggregatedModerationConfig:
    cpu_count = max(1, os.cpu_count() or 4)

    free_min_workers = 1
    free_max_workers = max(2, min(4, cpu_count))
    free_backlog_target = max(30, cpu_count * 3)
    free_backlog_low = max(10, int(free_backlog_target * 0.4))
    free_backlog_soft = max(200, int(free_backlog_target * 2.5))
    free_catchup = max(24, int(free_backlog_target * 1.5))

    accelerated_min_workers = 2
    accelerated_max_workers = max(5, cpu_count * 3)
    accelerated_backlog_target = 0
    accelerated_backlog_low = 0
    accelerated_backlog_soft = max(3, accelerated_min_workers * 2)
    accelerated_catchup = max(3, accelerated_min_workers * 2)

    free_policy = AdaptiveQueuePolicy(
        name="free",
        min_workers=free_min_workers,
        max_workers=free_max_workers,
        backlog_target=free_backlog_target,
        backlog_low=_clamp_int(free_backlog_low, minimum=1, maximum=free_backlog_target),
        backlog_soft_limit=free_backlog_soft,
        catchup_batch=free_catchup,
        provision_bias=0.65,
        recovery_bias=1.05,
        wait_threshold=25.0,
        min_runtime=0.45,
        maintain_backlog=True,
    )

    accelerated_policy = AdaptiveQueuePolicy(
        name="accelerated",
        min_workers=accelerated_min_workers,
        max_workers=accelerated_max_workers,
        backlog_target=accelerated_backlog_target,
        backlog_low=accelerated_backlog_low,
        backlog_soft_limit=accelerated_backlog_soft,
        catchup_batch=accelerated_catchup,
        provision_bias=1.15,
        recovery_bias=1.35,
        wait_threshold=4.5,
        min_runtime=0.2,
        maintain_backlog=False,
    )

    controller = AdaptiveControllerConfig(
        tick_interval=2.0,
        rate_window=180.0,
        scale_down_cooldown=25.0,
    )

    monitor = MonitorConfig()
    perf_monitor = PerformanceMonitorConfig()

    return AggregatedModerationConfig(
        free_policy=free_policy,
        accelerated_policy=accelerated_policy,
        controller=controller,
        monitor=monitor,
        performance_monitor=perf_monitor,
    )


__all__ = [
    "AdaptiveControllerConfig",
    "AdaptiveQueuePolicy",
    "AggregatedModerationConfig",
    "MonitorConfig",
    "PerformanceMonitorConfig",
    "load_config",
]
