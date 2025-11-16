from __future__ import annotations

from typing import Any, Dict, MutableMapping

from .serialization import coerce_int

__all__ = ["apply_count_baselines", "fetch_count_baselines"]


def _parse_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def fetch_count_baselines(client: Any, key: str) -> Dict[str, int]:
    """Load stored baseline counts for the provided hash key."""
    baseline_key = f"{key}:baseline"
    raw_mapping = await client.hgetall(baseline_key)
    baselines: dict[str, int] = {}
    for field, raw_value in raw_mapping.items():
        parsed = _parse_int(raw_value)
        if parsed is None:
            continue
        baselines[field] = parsed
    return baselines


def apply_count_baselines(payload: MutableMapping[str, Any], baselines: Mapping[str, int]) -> None:
    """Mutate payload so that baseline-tracked counters represent deltas since the last reset."""
    if not baselines:
        return
    for field, baseline_value in baselines.items():
        if field not in payload:
            continue
        current_value = coerce_int(payload.get(field))
        delta = current_value - baseline_value
        if delta > 0:
            payload[field] = str(delta)
        elif delta == 0:
            payload[field] = "0"
        # If delta < 0 we keep the original value to avoid negative counters.
