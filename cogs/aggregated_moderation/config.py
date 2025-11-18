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
    accelerated_text_policy: AdaptiveQueuePolicy
    video_policy: AdaptiveQueuePolicy
    controller: AdaptiveControllerConfig
    monitor: MonitorConfig
    performance_monitor: PerformanceMonitorConfig


def _clamp_int(value: int, *, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


def _build_policy(
    *,
    name: str,
    min_workers: int,
    max_workers: int,
    backlog_target: int = 0,
    backlog_low: int = 0,
    backlog_soft_limit: int,
    catchup_batch: int,
    provision_bias: float,
    recovery_bias: float,
    wait_threshold: float,
    min_runtime: float,
    maintain_backlog: bool = False,
) -> AdaptiveQueuePolicy:
    return AdaptiveQueuePolicy(
        name=name,
        min_workers=min_workers,
        max_workers=max_workers,
        backlog_target=backlog_target,
        backlog_low=backlog_low,
        backlog_soft_limit=backlog_soft_limit,
        catchup_batch=catchup_batch,
        provision_bias=provision_bias,
        recovery_bias=recovery_bias,
        wait_threshold=wait_threshold,
        min_runtime=min_runtime,
        maintain_backlog=maintain_backlog,
    )


def load_config() -> AggregatedModerationConfig:
    cpu_count = max(1, os.cpu_count() or 4)

    free_min_workers = 1
    free_max_workers = max(2, min(3, max(1, cpu_count // 2)))
    free_backlog_target = max(80, cpu_count * 4)
    free_backlog_low = max(6, int(free_backlog_target * 0.3))
    free_backlog_soft = max(250, int(free_backlog_target * 2.5))
    free_catchup = max(16, int(free_backlog_target * 0.7))

    accelerated_min_workers = max(4, min(8, cpu_count))
    accelerated_max_workers = max(5, cpu_count * 3)
    accelerated_backlog_target = 0
    accelerated_backlog_low = 0
    accelerated_backlog_soft = max(8, accelerated_min_workers * 4)
    accelerated_catchup = max(6, accelerated_min_workers * 2)

    free_policy = _build_policy(
        name="free",
        min_workers=free_min_workers,
        max_workers=free_max_workers,
        backlog_target=free_backlog_target,
        backlog_low=_clamp_int(free_backlog_low, minimum=1, maximum=free_backlog_target),
        backlog_soft_limit=free_backlog_soft,
        catchup_batch=free_catchup,
        provision_bias=0.6,
        recovery_bias=1.2,
        wait_threshold=35.0,
        min_runtime=0.45,
        maintain_backlog=True,
    )

    accelerated_policy = _build_policy(
        name="accelerated",
        min_workers=accelerated_min_workers,
        max_workers=accelerated_max_workers,
        backlog_target=accelerated_backlog_target,
        backlog_low=accelerated_backlog_low,
        backlog_soft_limit=accelerated_backlog_soft,
        catchup_batch=accelerated_catchup,
        provision_bias=1.2,
        recovery_bias=1.5,
        wait_threshold=2.5,
        min_runtime=0.2,
    )

    accelerated_text_min_workers = 2
    accelerated_text_max_workers = max(5, cpu_count * 2)
    accelerated_text_soft = max(6, accelerated_text_min_workers * 3)

    accelerated_text_policy = _build_policy(
        name="accelerated_text",
        min_workers=accelerated_text_min_workers,
        max_workers=accelerated_text_max_workers,
        backlog_soft_limit=accelerated_text_soft,
        catchup_batch=max(3, accelerated_text_min_workers * 2),
        provision_bias=1.1,
        recovery_bias=1.3,
        wait_threshold=3.0,
        min_runtime=0.15,
    )

    video_min_workers = 1
    video_max_workers = max(3, cpu_count * 2)
    video_backlog_soft = max(6, video_min_workers * 3)
    video_policy = _build_policy(
        name="video",
        min_workers=video_min_workers,
        max_workers=video_max_workers,
        backlog_soft_limit=video_backlog_soft,
        catchup_batch=max(2, video_min_workers),
        provision_bias=1.3,
        recovery_bias=1.6,
        wait_threshold=6.0,
        min_runtime=0.5,
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
        accelerated_text_policy=accelerated_text_policy,
        video_policy=video_policy,
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
