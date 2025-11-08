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
class AggregatedModerationConfig:
    free_policy: AdaptiveQueuePolicy
    accelerated_policy: AdaptiveQueuePolicy
    controller: AdaptiveControllerConfig
    monitor: MonitorConfig


def _clamp_int(value: int, *, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


def load_config() -> AggregatedModerationConfig:
    cpu_count = max(1, os.cpu_count() or 4)

    free_min_workers = 1
    free_max_workers = max(6, cpu_count * 2)
    free_backlog_target = max(18, cpu_count * 2)
    free_backlog_low = max(6, int(free_backlog_target * 0.35))
    free_backlog_soft = max(50, int(free_backlog_target * 2.5))
    free_catchup = max(8, int(free_backlog_target * 0.75))

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
        provision_bias=0.82,
        recovery_bias=1.08,
        wait_threshold=18.0,
        min_runtime=0.35,
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

    return AggregatedModerationConfig(
        free_policy=free_policy,
        accelerated_policy=accelerated_policy,
        controller=controller,
        monitor=monitor,
    )


__all__ = [
    "AdaptiveControllerConfig",
    "AdaptiveQueuePolicy",
    "AggregatedModerationConfig",
    "MonitorConfig",
    "load_config",
]
