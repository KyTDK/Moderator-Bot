from __future__ import annotations

from datetime import date, datetime
from urllib.parse import quote, unquote

from ..config import get_metrics_redis_config


def _guild_token(guild_id: int | None) -> str:
    if guild_id in (None, 0):
        return "0"
    return str(int(guild_id))


def encode_content_type(content_type: str) -> str:
    sanitized = content_type or "unknown"
    return quote(sanitized, safe="")


def decode_content_type(content_token: str) -> str:
    return unquote(content_token)


def rollup_key(metric_date: date, guild_id: int | None, content_type: str) -> str:
    config = get_metrics_redis_config()
    guild_token = _guild_token(guild_id)
    return ":".join(
        (
            config.key_prefix,
            "rollup",
            metric_date.isoformat(),
            guild_token,
            encode_content_type(content_type),
        )
    )


def rollup_status_key(rollup_key_value: str) -> str:
    return f"{rollup_key_value}:status"


def rollup_index_key() -> str:
    return f"{get_metrics_redis_config().key_prefix}:rollups:index"


def rollup_guild_index_key(guild_id: int | None) -> str:
    config = get_metrics_redis_config()
    guild_token = _guild_token(guild_id)
    return f"{config.key_prefix}:rollups:index:guild:{guild_token}"


def totals_key() -> str:
    return f"{get_metrics_redis_config().key_prefix}:totals"


def totals_status_key() -> str:
    return f"{totals_key()}:status"


def parse_rollup_key(key: str) -> tuple[date, int | None, str] | None:
    try:
        prefix, date_part, guild_token, content_token = key.rsplit(":", 3)
    except ValueError:
        return None
    if not prefix.endswith(":rollup"):
        return None
    try:
        metric_date = datetime.strptime(date_part, "%Y-%m-%d").date()
    except ValueError:
        return None
    guild_id = int(guild_token)
    guild_value = None if guild_id == 0 else guild_id
    content_type = decode_content_type(content_token)
    return metric_date, guild_value, content_type


__all__ = [
    "decode_content_type",
    "encode_content_type",
    "parse_rollup_key",
    "rollup_guild_index_key",
    "rollup_index_key",
    "rollup_key",
    "rollup_status_key",
    "totals_key",
    "totals_status_key",
]
