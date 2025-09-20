from __future__ import annotations

import logging
import os
import socket
import uuid
from dataclasses import dataclass

from dotenv import load_dotenv

_logger = logging.getLogger(__name__)


def _parse_int(
    raw: str | None,
    *,
    default: int,
    minimum: int | None = None,
    name: str = "value",
) -> int:
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        _logger.warning("Invalid %s=%s; using %s", name, raw, default)
        return default
    if minimum is not None and value < minimum:
        _logger.warning("%s=%s below minimum %s; clamping", name, value, minimum)
        return minimum
    return value


@dataclass(slots=True)
class ShardConfig:
    total_shards: int
    preferred_shard: int | None
    stale_seconds: int
    heartbeat_seconds: int
    instance_id: str


@dataclass(slots=True)
class RuntimeConfig:
    token: str
    log_level: str
    log_cog_loads: bool
    shard: ShardConfig


TRUE_VALUES = {"1", "true", "yes", "on"}


def load_runtime_config() -> RuntimeConfig:
    load_dotenv()

    token = os.getenv("DISCORD_TOKEN", "")
    log_level = os.getenv("LOG_LEVEL", "WARNING").upper()

    total_shards = _parse_int(
        os.getenv("MODBOT_TOTAL_SHARDS") or os.getenv("DISCORD_TOTAL_SHARDS"),
        default=1,
        minimum=1,
        name="total_shards",
    )
    preferred_raw = os.getenv("MODBOT_PREFERRED_SHARD")
    preferred_shard: int | None
    if preferred_raw is None or preferred_raw == "":
        preferred_shard = None
    else:
        try:
            preferred_shard = int(preferred_raw)
        except ValueError:
            _logger.warning("Invalid MODBOT_PREFERRED_SHARD=%s; ignoring", preferred_raw)
            preferred_shard = None

    stale_seconds = _parse_int(
        os.getenv("MODBOT_SHARD_STALE_SECONDS"),
        default=300,
        minimum=60,
        name="MODBOT_SHARD_STALE_SECONDS",
    )
    heartbeat_seconds = _parse_int(
        os.getenv("MODBOT_SHARD_HEARTBEAT_SECONDS"),
        default=60,
        minimum=15,
        name="MODBOT_SHARD_HEARTBEAT_SECONDS",
    )
    instance_id = (
        os.getenv("MODBOT_INSTANCE_ID")
        or os.getenv("INSTANCE_ID")
        or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    )

    shard_config = ShardConfig(
        total_shards=total_shards,
        preferred_shard=preferred_shard,
        stale_seconds=stale_seconds,
        heartbeat_seconds=heartbeat_seconds,
        instance_id=instance_id,
    )

    log_cog_loads = os.getenv("LOG_COG_LOADS", "0").lower() in TRUE_VALUES

    return RuntimeConfig(
        token=token,
        log_level=log_level,
        log_cog_loads=log_cog_loads,
        shard=shard_config,
    )
