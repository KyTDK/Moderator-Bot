from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from typing import Optional

from modules.worker_queue import WorkerQueue

from .config import AdaptiveControllerConfig, AdaptiveQueuePolicy


@dataclass(frozen=True, slots=True)
class AdaptivePlan:
    target_workers: int
    baseline_workers: int
    backlog_high: Optional[int]
    backlog_low: Optional[int]
    backlog_hard_limit: Optional[int]
    backlog_shed_to: Optional[int]


@dataclass(slots=True)
class _QueueState:
    queue: WorkerQueue
    policy: AdaptiveQueuePolicy
    last_plan: Optional[AdaptivePlan] = None
    last_change_at: float = 0.0


class AdaptiveQueueController:
    def __init__(
        self,
        *,
        free_queue: WorkerQueue,
        accelerated_queue: WorkerQueue,
        free_policy: AdaptiveQueuePolicy,
        accelerated_policy: AdaptiveQueuePolicy,
        config: AdaptiveControllerConfig,
    ) -> None:
        self._config = config
        self._states = {
            "free": _QueueState(queue=free_queue, policy=free_policy),
            "accelerated": _QueueState(queue=accelerated_queue, policy=accelerated_policy),
        }
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        loop = asyncio.get_running_loop()
        self._task = loop.create_task(self._run(), name="adaptive_queue_controller")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    async def _run(self) -> None:
        await asyncio.sleep(self._config.tick_interval)
        try:
            while True:
                await asyncio.sleep(self._config.tick_interval)
                now = time.monotonic()
                for state in self._states.values():
                    metrics = state.queue.metrics()
                    plan = self._build_plan(metrics, state.policy)
                    await self._apply_plan_if_needed(state, plan, now)
        except asyncio.CancelledError:
            raise

    async def _apply_plan_if_needed(self, state: _QueueState, plan: AdaptivePlan, now: float) -> None:
        previous = state.last_plan
        applied_plan = plan

        if previous is not None:
            if (
                previous.target_workers == plan.target_workers
                and previous.baseline_workers == plan.baseline_workers
                and previous.backlog_high == plan.backlog_high
                and previous.backlog_low == plan.backlog_low
                and previous.backlog_hard_limit == plan.backlog_hard_limit
                and previous.backlog_shed_to == plan.backlog_shed_to
            ):
                return

            scaling_down = plan.target_workers < previous.target_workers
            if scaling_down and (now - state.last_change_at) < self._config.scale_down_cooldown:
                applied_plan = AdaptivePlan(
                    target_workers=previous.target_workers,
                    baseline_workers=max(plan.baseline_workers, previous.baseline_workers),
                    backlog_high=plan.backlog_high,
                    backlog_low=plan.backlog_low,
                    backlog_hard_limit=plan.backlog_hard_limit,
                    backlog_shed_to=plan.backlog_shed_to,
                )

        await state.queue.update_adaptive_plan(
            target_workers=applied_plan.target_workers,
            baseline_workers=applied_plan.baseline_workers,
            backlog_high=applied_plan.backlog_high,
            backlog_low=applied_plan.backlog_low,
            backlog_hard_limit=applied_plan.backlog_hard_limit,
            backlog_shed_to=applied_plan.backlog_shed_to,
        )
        state.last_plan = applied_plan
        state.last_change_at = now

    def _build_plan(self, metrics: dict, policy: AdaptiveQueuePolicy) -> AdaptivePlan:
        backlog = int(metrics.get("backlog") or 0)
        arrival_rate = float(metrics.get("arrival_rate_per_min") or 0.0)
        completion_rate = float(metrics.get("completion_rate_per_min") or 0.0)
        ema_runtime = float(metrics.get("ema_runtime") or 0.0)
        avg_runtime = float(metrics.get("avg_runtime") or 0.0)
        runtime = ema_runtime or avg_runtime
        if runtime <= 0.0:
            runtime = policy.min_runtime
        else:
            runtime = max(policy.min_runtime, runtime)
        per_worker_capacity = 60.0 / runtime if runtime > 0.0 else 60.0 / policy.min_runtime

        busy_workers = max(1, int(metrics.get("busy_workers") or 0))
        if completion_rate > 0.0:
            observed_capacity = completion_rate / busy_workers
            per_worker_capacity = max(per_worker_capacity, observed_capacity)

        wait_signal = max(
            float(metrics.get("ema_wait_time") or 0.0),
            float(metrics.get("avg_wait_time") or 0.0),
            float(metrics.get("last_wait_time") or 0.0),
        )

        bias = policy.provision_bias
        if wait_signal >= policy.wait_threshold or backlog > policy.backlog_soft_limit:
            bias = max(bias, policy.recovery_bias)

        demand_workers = 0
        if per_worker_capacity > 0.0:
            demand_workers = math.ceil((arrival_rate * bias) / per_worker_capacity)

        backlog_source = backlog - policy.backlog_target if policy.maintain_backlog else backlog
        backlog_excess = max(0, backlog_source)
        backlog_pressure = math.ceil(backlog_excess / max(1, policy.catchup_batch))

        target_workers = max(policy.min_workers, demand_workers + backlog_pressure)
        if arrival_rate <= 0.1 and backlog <= policy.backlog_target:
            target_workers = policy.min_workers

        target_workers = min(policy.max_workers, target_workers)

        baseline_workers = max(1, min(target_workers, policy.min_workers))

        dynamic_high = max(
            policy.backlog_soft_limit,
            policy.backlog_target,
            target_workers * max(1, policy.catchup_batch),
        )
        if policy.maintain_backlog:
            backlog_high = dynamic_high
        else:
            backlog_high = max(dynamic_high, policy.catchup_batch)

        backlog_low = None
        if policy.backlog_low > 0:
            backlog_low = min(policy.backlog_low, max(0, backlog_high - policy.catchup_batch))
        elif not policy.maintain_backlog:
            backlog_low = 0

        backlog_hard = max(backlog_high * 2, backlog_high + policy.catchup_batch)
        shed_target = max(policy.backlog_target, backlog_high)

        return AdaptivePlan(
            target_workers=target_workers,
            baseline_workers=baseline_workers,
            backlog_high=backlog_high,
            backlog_low=backlog_low,
            backlog_hard_limit=backlog_hard,
            backlog_shed_to=shed_target,
        )


__all__ = ["AdaptiveQueueController", "AdaptivePlan"]
