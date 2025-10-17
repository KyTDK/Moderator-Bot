from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class MetricsRedisConfig:
    url: str | None
    stream_name: str
    stream_maxlen: int | None
    stream_approximate: bool
    key_prefix: str

    @property
    def enabled(self) -> bool:
        return bool(self.url)


def _parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _parse_bool(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    value_normalized = value.strip().lower()
    if value_normalized in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if value_normalized in {"0", "false", "f", "no", "n", "off"}:
        return False
    return default


@lru_cache(maxsize=1)
def get_metrics_redis_config() -> MetricsRedisConfig:
    url = os.getenv("METRICS_REDIS_URL") or os.getenv("REDIS_URL")
    stream_name = os.getenv("METRICS_REDIS_STREAM", "moderator:metrics")
    maxlen_env = os.getenv("METRICS_REDIS_STREAM_MAXLEN")
    stream_maxlen = _parse_int(maxlen_env)
    approximate = _parse_bool(os.getenv("METRICS_REDIS_STREAM_APPROX", "true"), default=True)
    key_prefix = os.getenv("METRICS_REDIS_PREFIX", "moderator:metrics")
    return MetricsRedisConfig(
        url=url.strip() if isinstance(url, str) and url.strip() else None,
        stream_name=stream_name.strip() or "moderator:metrics",
        stream_maxlen=stream_maxlen,
        stream_approximate=approximate,
        key_prefix=key_prefix.strip() or "moderator:metrics",
    )
