from __future__ import annotations

import asyncio

from redis.asyncio import Redis

from modules.metrics.backend.keys import parse_rollup_key, rollup_guild_index_key


async def backfill_global_rollup_metric_dates() -> None:
    client = Redis(host="127.0.0.1", port=6379, decode_responses=True)
    index_key = rollup_guild_index_key(None)
    updated = 0

    try:
        rollup_keys = await client.zrange(index_key, 0, -1)

        for key_str in rollup_keys:
            parsed = parse_rollup_key(key_str)
            if not parsed:
                continue

            metric_date, _, _ = parsed
            metric_date_iso = metric_date.isoformat()

            existing = await client.hget(key_str, "metric_date")
            if existing == metric_date_iso:
                continue

            await client.hset(key_str, mapping={"metric_date": metric_date_iso})
            updated += 1
    finally:
        await client.close()
        await client.connection_pool.disconnect()

    print(f"Updated metric_date for {updated} rollup hashes in {index_key}.")


async def main() -> None:
    await backfill_global_rollup_metric_dates()


if __name__ == "__main__":
    asyncio.run(main())
