from __future__ import annotations

import asyncio

from modules.metrics.backend import close_metrics_client, get_redis_client
from modules.metrics.backend.keys import parse_rollup_key, rollup_guild_index_key


async def backfill_global_rollup_metric_dates() -> None:
    client = await get_redis_client()
    index_key = rollup_guild_index_key(None)

    rollup_keys = await client.zrange(index_key, 0, -1)
    updated = 0

    for key in rollup_keys:
        key_str = key.decode("utf-8") if isinstance(key, (bytes, bytearray)) else key
        parsed = parse_rollup_key(key_str)
        if not parsed:
            continue

        metric_date, _, _ = parsed
        metric_date_iso = metric_date.isoformat()

        existing = await client.hget(key_str, "metric_date")
        if isinstance(existing, (bytes, bytearray)):
            existing = existing.decode("utf-8")

        if existing == metric_date_iso:
            continue

        await client.hset(key_str, mapping={"metric_date": metric_date_iso})
        updated += 1

    await close_metrics_client()

    print(f"Updated metric_date for {updated} rollup hashes in {index_key}.")


async def main() -> None:
    await backfill_global_rollup_metric_dates()


if __name__ == "__main__":
    asyncio.run(main())
