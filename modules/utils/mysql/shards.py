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

_logger = logging.getLogger(__name__)

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
    instance_stale_after_seconds: int | None = None,
) -> ShardAssignment:
    """Claim a shard row, ensuring no other live instance owns it."""
    if total_shards < 1:
        raise ValueError("total_shards must be >= 1")

    await ensure_shard_records(total_shards)

    if instance_stale_after_seconds is not None and instance_stale_after_seconds < 1:
        raise ValueError("instance_stale_after_seconds must be >= 1")

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.begin()
        async with conn.cursor(aiomysql.DictCursor) as cur:
            conditions = [
                "last_heartbeat IS NULL",
                "last_heartbeat < UTC_TIMESTAMP() - INTERVAL %s SECOND",
                "(status = 'starting' AND claimed_at < UTC_TIMESTAMP() - INTERVAL %s SECOND)",
            ]
            params: list[int] = [stale_after_seconds, stale_after_seconds]

            if instance_stale_after_seconds is not None:
                conditions.append(
                    "NOT EXISTS ("
                    "    SELECT 1"
                    "    FROM bot_instances bi"
                    "    WHERE bi.instance_id = bot_shards.claimed_by"
                    "      AND bi.last_seen >= UTC_TIMESTAMP() - INTERVAL %s SECOND"
                    ")"
                )
                params.append(instance_stale_after_seconds)

            condition_sql = " OR ".join(f"({clause})" for clause in conditions)

            await cur.execute(
                f"""
                UPDATE bot_shards
                SET status = 'available',
                    claimed_by = NULL,
                    claimed_at = NULL,
                    last_heartbeat = NULL,
                    session_id = NULL,
                    resume_gateway_url = NULL,
                    last_error = NULL
                WHERE claimed_by IS NOT NULL
                  AND ({condition_sql})
                """,
                tuple(params),
            )

            stale_released = cur.rowcount
            if stale_released:
                _logger.info(
                    "Released %s stale shard(s) before claiming for %s",
                    stale_released,
                    instance_id,
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
                    _logger.warning(
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
                await cur.execute(
                    """
                    SELECT status, COUNT(*) AS count
                    FROM bot_shards
                    GROUP BY status
                    ORDER BY status
                    """
                )
                status_counts = await cur.fetchall()

                await cur.execute(
                    """
                    SELECT shard_id,
                           status,
                           claimed_by,
                           TIMESTAMPDIFF(SECOND, last_heartbeat, UTC_TIMESTAMP()) AS heartbeat_age,
                           TIMESTAMPDIFF(SECOND, claimed_at, UTC_TIMESTAMP()) AS claim_age
                    FROM bot_shards
                    ORDER BY shard_id
                    LIMIT 5
                    """
                )
                sample_rows = await cur.fetchall()

                await conn.rollback()

                status_counts_text = (
                    ", ".join(
                        f"{row['status'] or 'NULL'}={row['count']}"
                        for row in status_counts
                    )
                    or "empty"
                )
                sample_preview = [
                    {
                        "id": row["shard_id"],
                        "status": row["status"],
                        "claimed_by": row["claimed_by"],
                        "heartbeat_age": row["heartbeat_age"],
                        "claim_age": row["claim_age"],
                    }
                    for row in sample_rows
                ]

                _logger.warning(
                    "Shard claim failed for %s; no free shards. Status counts: %s; sample: %s",
                    instance_id,
                    status_counts_text,
                    sample_preview,
                )

                raise ShardClaimError(
                    f"No free shards available to claim (status counts: {status_counts_text})"
                )

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

async def recover_stuck_shards(
    shard_stale_after_seconds: int,
    *,
    instance_stale_after_seconds: int | None = None,
) -> int:
    """Force release shard rows whose owners have stopped reporting in time."""

    if shard_stale_after_seconds < 1:
        raise ValueError("shard_stale_after_seconds must be >= 1")
    if instance_stale_after_seconds is not None and instance_stale_after_seconds < 1:
        raise ValueError("instance_stale_after_seconds must be >= 1")

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            conditions = [
                "last_heartbeat IS NULL",
                "last_heartbeat < UTC_TIMESTAMP() - INTERVAL %s SECOND",
                "(status = 'starting' AND claimed_at < UTC_TIMESTAMP() - INTERVAL %s SECOND)",
            ]
            params: list[int] = [
                shard_stale_after_seconds,
                shard_stale_after_seconds,
            ]

            if instance_stale_after_seconds is not None:
                conditions.append(
                    "NOT EXISTS ("
                    "    SELECT 1"
                    "    FROM bot_instances bi"
                    "    WHERE bi.instance_id = bot_shards.claimed_by"
                    "      AND bi.last_seen >= UTC_TIMESTAMP() - INTERVAL %s SECOND"
                    ")"
                )
                params.append(instance_stale_after_seconds)

            condition_sql = " OR ".join(f"({clause})" for clause in conditions)

            await cur.execute(
                f"""
                UPDATE bot_shards
                SET status = 'available',
                    claimed_by = NULL,
                    claimed_at = NULL,
                    last_heartbeat = NULL,
                    session_id = NULL,
                    resume_gateway_url = NULL,
                    last_error = NULL
                WHERE claimed_by IS NOT NULL
                  AND ({condition_sql})
                """,
                tuple(params),
            )

            released = cur.rowcount

            if instance_stale_after_seconds is not None:
                await cur.execute(
                    """
                    DELETE FROM bot_instances
                    WHERE last_seen < UTC_TIMESTAMP() - INTERVAL %s SECOND
                    """,
                    (instance_stale_after_seconds,),
                )

            await conn.commit()

            if released:
                _logger.info(
                    "Released %s shard(s) from stalled owners", released
                )

            return released


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

