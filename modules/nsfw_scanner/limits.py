from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from modules.config.premium_plans import (
    PLAN_CORE,
    PLAN_FREE,
    PLAN_PRO,
    PLAN_ULTRA,
    normalize_plan_name,
)

from .constants import (
    ACCELERATED_DOWNLOAD_CAP_BYTES,
    ACCELERATED_MAX_CONCURRENT_FRAMES,
    ACCELERATED_MAX_FRAMES_PER_VIDEO,
    ACCELERATED_PRO_CONCURRENT_FRAMES,
    ACCELERATED_PRO_DOWNLOAD_CAP_BYTES,
    ACCELERATED_PRO_MAX_FRAMES_PER_VIDEO,
    ACCELERATED_ULTRA_CONCURRENT_FRAMES,
    ACCELERATED_ULTRA_DOWNLOAD_CAP_BYTES,
    ACCELERATED_ULTRA_MAX_FRAMES_PER_VIDEO,
    DEFAULT_DOWNLOAD_CAP_BYTES,
    MAX_CONCURRENT_FRAMES,
    MAX_FRAMES_PER_VIDEO,
    MOD_API_CONCURRENCY_BY_PLAN,
)


@dataclass(frozen=True, slots=True)
class PremiumLimits:
    plan: str
    download_cap_bytes: int | None
    max_downloads: int
    max_frame_decodes: int
    max_moderation_calls: int
    max_frames_in_flight: int | None
    max_frames_per_video: int | None
    max_video_workers: int
    max_vector_batch: int
    clip_batch_size: int
    max_media_tasks: int

    @property
    def is_premium(self) -> bool:
        return self.plan != PLAN_FREE


def _build_limits() -> dict[str, PremiumLimits]:
    plan_concurrency = MOD_API_CONCURRENCY_BY_PLAN
    free_mod_calls = max(1, plan_concurrency.get(PLAN_FREE, 3))
    core_mod_calls = max(free_mod_calls, plan_concurrency.get(PLAN_CORE, free_mod_calls))
    pro_mod_calls = max(core_mod_calls, plan_concurrency.get(PLAN_PRO, core_mod_calls))
    ultra_mod_calls = max(pro_mod_calls, plan_concurrency.get(PLAN_ULTRA, pro_mod_calls))
    return {
        PLAN_FREE: PremiumLimits(
            plan=PLAN_FREE,
            download_cap_bytes=DEFAULT_DOWNLOAD_CAP_BYTES,
            max_downloads=4,
            max_frame_decodes=6,
            max_moderation_calls=free_mod_calls,
            max_frames_in_flight=MAX_CONCURRENT_FRAMES,
            max_frames_per_video=MAX_FRAMES_PER_VIDEO,
            max_video_workers=2,
            max_vector_batch=4,
            clip_batch_size=4,
            max_media_tasks=8,
        ),
        PLAN_CORE: PremiumLimits(
            plan=PLAN_CORE,
            download_cap_bytes=ACCELERATED_DOWNLOAD_CAP_BYTES,
            max_downloads=8,
            max_frame_decodes=12,
            max_moderation_calls=core_mod_calls,
            max_frames_in_flight=ACCELERATED_MAX_CONCURRENT_FRAMES,
            max_frames_per_video=ACCELERATED_MAX_FRAMES_PER_VIDEO,
            max_video_workers=4,
            max_vector_batch=8,
            clip_batch_size=8,
            max_media_tasks=14,
        ),
        PLAN_PRO: PremiumLimits(
            plan=PLAN_PRO,
            download_cap_bytes=ACCELERATED_PRO_DOWNLOAD_CAP_BYTES,
            max_downloads=10,
            max_frame_decodes=18,
            max_moderation_calls=pro_mod_calls,
            max_frames_in_flight=ACCELERATED_PRO_CONCURRENT_FRAMES,
            max_frames_per_video=ACCELERATED_PRO_MAX_FRAMES_PER_VIDEO,
            max_video_workers=5,
            max_vector_batch=12,
            clip_batch_size=12,
            max_media_tasks=18,
        ),
        PLAN_ULTRA: PremiumLimits(
            plan=PLAN_ULTRA,
            download_cap_bytes=ACCELERATED_ULTRA_DOWNLOAD_CAP_BYTES,
            max_downloads=14,
            max_frame_decodes=24,
            max_moderation_calls=ultra_mod_calls,
            max_frames_in_flight=ACCELERATED_ULTRA_CONCURRENT_FRAMES,
            max_frames_per_video=ACCELERATED_ULTRA_MAX_FRAMES_PER_VIDEO,
            max_video_workers=6,
            max_vector_batch=18,
            clip_batch_size=16,
            max_media_tasks=28,
        ),
    }


_LIMITS_BY_PLAN = _build_limits()
@lru_cache(maxsize=8)
def resolve_limits(plan: str | None) -> PremiumLimits:
    normalized = normalize_plan_name(plan, default=PLAN_FREE)
    limits = _LIMITS_BY_PLAN.get(normalized)
    if limits is None:
        limits = _LIMITS_BY_PLAN[PLAN_FREE]
    return limits


__all__ = ["PremiumLimits", "resolve_limits"]
