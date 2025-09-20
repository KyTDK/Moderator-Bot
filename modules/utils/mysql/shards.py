from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import aiomysql

from .connection import get_pool

class ShardClaimError(RuntimeError):
    """Raised when the allocator cannot hand out a shard."""

@dataclass(slots=True)
class ShardAssignment:
    shard_id: int
    shard_count: int

async def ensure_shard_records(total_shards: int) -> None:
    """Guarantee that placeholder rows exist for the expected shard range."""
    if total_shards < 1:
        raise ValueError("total_shards must be >= 1")

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            values = [(idx, idx) for idx in range(total_shards)]
            if values:
                await cur.executemany(
                    "INSERT INTO bot_shards (shard_id) SELECT %s WHERE NOT EXISTS (SELECT 1 FROM bot_shards WHERE shard_id = %s)",
                    values,
                )
                await conn.commit()

async def claim_shard(
    instance_id: str,
    *,
    total_shards: int,
    preferred_shard: Optional[int] = None,
    stale_after_seconds: int = 300,
) -> ShardAssignment:
    """Claim a shard row, ensuring no other live instance owns it."""
    if total_shards < 1:
        raise ValueError("total_shards must be >= 1")

    await ensure_shard_records(total_shards)

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.begin()
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """
                UPDATE bot_shards
                SET status = 'available',
                    claimed_by = NULL,
                    claimed_at = NULL,
                    last_heartbeat = NULL,
                    session_id = NULL,
                    resume_gateway_url = NULL,
                    last_error = NULL
                WHERE claimed_by IS NOT NULL
                  AND (
                        last_heartbeat IS NULL
                     OR last_heartbeat < UTC_TIMESTAMP() - INTERVAL %s SECOND
                     OR (status = 'starting' AND claimed_at < UTC_TIMESTAMP() - INTERVAL %s SECOND)
                  )
                """,
                (stale_after_seconds, stale_after_seconds),
            )

            candidate: Optional[dict] = None

            await cur.execute(
                """
                SELECT shard_id
                FROM bot_shards
                WHERE claimed_by = %s
                LIMIT 1
                FOR UPDATE
                """,
                (instance_id,),
            )
            row = await cur.fetchone()
            if row:
                candidate = row

            if candidate is None and preferred_shard is not None:
                if 0 <= preferred_shard < total_shards:
                    await cur.execute(
                        """
                        SELECT shard_id
                        FROM bot_shards
                        WHERE shard_id = %s
                          AND status = 'available'
                        LIMIT 1
                        FOR UPDATE
                        """,
                        (preferred_shard,),
                    )
                    row = await cur.fetchone()
                    if row:
                        candidate = row
                else:
                    logging.warning(
                        "Preferred shard %s is outside configured total (%s)",
                        preferred_shard,
                        total_shards,
                    )

            if candidate is None:
                await cur.execute(
                    """
                    SELECT shard_id
                    FROM bot_shards
                    WHERE status = 'available'
                    ORDER BY shard_id ASC
                    LIMIT 1
                    FOR UPDATE
                    """
                )
                row = await cur.fetchone()
                if row:
                    candidate = row

            if candidate is None:
                await conn.rollback()
                raise ShardClaimError("No free shards available to claim")

            shard_id = int(candidate["shard_id"])

            await cur.execute(
                """
                UPDATE bot_shards
                SET status = 'starting',
                    claimed_by = %s,
                    claimed_at = UTC_TIMESTAMP(),
                    last_heartbeat = UTC_TIMESTAMP(),
                    session_id = NULL,
                    resume_gateway_url = NULL,
                    last_error = NULL
                WHERE shard_id = %s
                """,
                (instance_id, shard_id),
            )

            if cur.rowcount != 1:
                await conn.rollback()
                raise ShardClaimError(f"Failed to update shard {shard_id}")

        await conn.commit()

    return ShardAssignment(shard_id=shard_id, shard_count=total_shards)

async def update_shard_status(
    shard_id: int,
    instance_id: str,
    *,
    status: str,
    session_id: Optional[str] = None,
    resume_url: Optional[str] = None,
    last_error: Optional[str] = None,
) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE bot_shards
                SET status = %s,
                    last_heartbeat = UTC_TIMESTAMP(),
                    session_id = %s,
                    resume_gateway_url = %s,
                    last_error = %s
                WHERE shard_id = %s
                  AND claimed_by = %s
                """,
                (status, session_id, resume_url, last_error, shard_id, instance_id),
            )
            await conn.commit()
            return cur.rowcount == 1

async def release_shard(shard_id: int, instance_id: str, *, clear_error: bool = False) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            assignments = [
                "status = 'available'",
                "claimed_by = NULL",
                "claimed_at = NULL",
                "last_heartbeat = NULL",
                "session_id = NULL",
                "resume_gateway_url = NULL",
            ]
            if clear_error:
                assignments.append("last_error = NULL")
            query = """
                UPDATE bot_shards
                SET {assignments}
                WHERE shard_id = %s
                  AND claimed_by = %s
                """.format(assignments=', '.join(assignments))
            await cur.execute(query, (shard_id, instance_id))
            await conn.commit()
            return cur.rowcount == 1
