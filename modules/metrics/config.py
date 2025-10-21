from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import parse_qsl, urlparse, urlunparse, urlencode

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


def _ensure_metrics_db(url: str | None, *, db_index: int = 1) -> str | None:
    if not isinstance(url, str):
        return None

    trimmed = url.strip()
    if not trimmed:
        return None

    parsed = urlparse(trimmed)

    # redis:// connections use the path component for the database index.
    if parsed.netloc:
        new_path = f"/{db_index}"
        parsed = parsed._replace(path=new_path)
        if parsed.query:
            filtered_query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "db"]
            parsed = parsed._replace(query=urlencode(filtered_query))
        return urlunparse(parsed)

    # redis+unix:// style URLs omit the netloc and rely on the db query parameter.
    query_items = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "db"]
    query_items.append(("db", str(db_index)))
    return urlunparse(parsed._replace(query=urlencode(query_items)))


@lru_cache(maxsize=1)
def get_metrics_redis_config() -> MetricsRedisConfig:
    url = _ensure_metrics_db(os.getenv("METRICS_REDIS_URL"))
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
