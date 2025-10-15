from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

from modules.metrics.models import ModerationMetric

from .connection import execute_query, get_pool


def _normalise_timestamp(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _parse_details(raw: str | bytes | None) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="ignore")
    try:
        value = json.loads(raw)
    except Exception:
        return {"__raw__": raw}
    if isinstance(value, dict):
        return value
    return {"__raw__": value}


def _metric_rollup_date(metric: ModerationMetric) -> date:
    return metric.occurred_at.astimezone(timezone.utc).date()


def _metric_rollup_guild(metric: ModerationMetric) -> int:
    try:
        return int(metric.guild_id or 0)
    except Exception:
        return 0


async def _update_metric_rollup(metric: ModerationMetric) -> None:
    flagged_increment = 1 if metric.was_flagged else 0
    last_flagged_at = _normalise_timestamp(metric.occurred_at) if metric.was_flagged else None
    last_reference = metric.reference if metric.was_flagged else None

    await execute_query(
        """
        INSERT INTO moderation_metric_rollups (
            metric_date,
            guild_id,
            content_type,
            scans_count,
            flagged_count,
            last_flagged_at,
            last_reference
        )
        VALUES (%s, %s, %s, 1, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            scans_count = scans_count + 1,
            flagged_count = flagged_count + VALUES(flagged_count),
            last_flagged_at = CASE
                WHEN VALUES(flagged_count) > 0 THEN VALUES(last_flagged_at)
                ELSE last_flagged_at
            END,
            last_reference = CASE
                WHEN VALUES(flagged_count) > 0 THEN VALUES(last_reference)
                ELSE last_reference
            END
        """,
        (
            _metric_rollup_date(metric),
            _metric_rollup_guild(metric),
            metric.content_type,
            flagged_increment,
            last_flagged_at,
            last_reference,
        ),
    )


async def insert_moderation_metric(metric: ModerationMetric) -> int:
    """Persist a moderation metric record and return the inserted ID."""

    params = metric.to_mysql_params()
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO moderation_metrics (
                    occurred_at,
                    guild_id,
                    channel_id,
                    user_id,
                    message_id,
                    content_type,
                    event_type,
                    was_flagged,
                    flags_count,
                    primary_reason,
                    details,
                    scan_duration_ms,
                    scanner,
                    source,
                    reference
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                params,
            )
            await conn.commit()
            last_row_id = cur.lastrowid or 0
    await _update_metric_rollup(metric)
    return int(last_row_id)


async def fetch_recent_metrics(
    *,
    guild_id: int | None = None,
    limit: int = 100,
    since: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return the most recent moderation metrics for analysis dashboards."""

    where_clauses: list[str] = []
    params: list[Any] = []
    if guild_id is not None:
        where_clauses.append("guild_id = %s")
        params.append(guild_id)
    if since is not None:
        where_clauses.append("occurred_at >= %s")
        params.append(_normalise_timestamp(since))

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    query = f"""
        SELECT
            id,
            occurred_at,
            guild_id,
            channel_id,
            user_id,
            message_id,
            content_type,
            event_type,
            was_flagged,
            flags_count,
            primary_reason,
            details,
            scan_duration_ms,
            scanner,
            source,
            reference
        FROM moderation_metrics
        {where_sql}
        ORDER BY occurred_at DESC
        LIMIT %s
    """
    params.append(int(limit))

    rows, _ = await execute_query(
        query,
        tuple(params),
        fetch_all=True,
    )

    metrics: list[dict[str, Any]] = []
    for row in rows or []:
        (
            metric_id,
            occurred_at,
            guild_id_raw,
            channel_id,
            user_id,
            message_id,
            content_type,
            event_type,
            was_flagged,
            flags_count,
            primary_reason,
            details,
            scan_duration_ms,
            scanner,
            source,
            reference,
        ) = row
        metrics.append(
            {
                "id": int(metric_id),
                "occurred_at": occurred_at.replace(tzinfo=timezone.utc) if isinstance(occurred_at, datetime) else occurred_at,
                "guild_id": int(guild_id_raw) if guild_id_raw is not None else None,
                "channel_id": int(channel_id) if channel_id is not None else None,
                "user_id": int(user_id) if user_id is not None else None,
                "message_id": int(message_id) if message_id is not None else None,
                "content_type": content_type,
                "event_type": event_type,
                "was_flagged": bool(was_flagged),
                "flags_count": int(flags_count or 0),
                "primary_reason": primary_reason,
                "details": _parse_details(details),
                "scan_duration_ms": int(scan_duration_ms) if scan_duration_ms is not None else None,
                "scanner": scanner,
                "source": source,
                "reference": reference,
            }
        )
    return metrics


async def summarise_metrics(
    *,
    guild_id: int | None = None,
    since: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return aggregate counts grouped by content type for dashboards."""

    where_clauses: list[str] = []
    params: list[Any] = []
    if guild_id is not None:
        where_clauses.append("guild_id = %s")
        params.append(guild_id)
    if since is not None:
        where_clauses.append("occurred_at >= %s")
        params.append(_normalise_timestamp(since))

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    rows, _ = await execute_query(
        f"""
        SELECT
            content_type,
            COUNT(*) AS total_scans,
            SUM(CASE WHEN was_flagged THEN 1 ELSE 0 END) AS flagged_scans,
            SUM(flags_count) AS total_flags,
            SUM(CASE WHEN was_flagged THEN flags_count ELSE 0 END) AS flags_on_flagged
        FROM moderation_metrics
        {where_sql}
        GROUP BY content_type
        ORDER BY total_scans DESC
        """,
        tuple(params),
        fetch_all=True,
    )

    summary: list[dict[str, Any]] = []
    for row in rows or []:
        (
            content_type,
            total_scans,
            flagged_scans,
            total_flags,
            flags_on_flagged,
        ) = row
        summary.append(
            {
                "content_type": content_type,
                "total_scans": int(total_scans or 0),
                "flagged_scans": int(flagged_scans or 0),
                "total_flags": int(total_flags or 0),
                "flags_on_flagged": int(flags_on_flagged or 0),
            }
        )
    return summary


async def fetch_metric_rollups(
    *,
    guild_id: int | None = None,
    content_type: str | None = None,
    since: date | datetime | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Return pre-aggregated moderation rollups (per day, per guild/content type)."""

    where_clauses: list[str] = []
    params: list[Any] = []
    if guild_id is not None:
        where_clauses.append("guild_id = %s")
        params.append(int(guild_id))
    if content_type:
        where_clauses.append("content_type = %s")
        params.append(content_type)
    if since is not None:
        if isinstance(since, datetime):
            metric_since = since.date()
        else:
            metric_since = since
        where_clauses.append("metric_date >= %s")
        params.append(metric_since)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    params_with_limit = tuple(params + [int(limit)])
    rows, _ = await execute_query(
        f"""
        SELECT
            metric_date,
            guild_id,
            content_type,
            scans_count,
            flagged_count,
            last_flagged_at,
            last_reference
        FROM moderation_metric_rollups
        {where_sql}
        ORDER BY metric_date DESC
        LIMIT %s
        """,
        params_with_limit,
        fetch_all=True,
    )

    rollups: list[dict[str, Any]] = []
    for row in rows or []:
        (
            metric_date,
            guild_raw,
            content,
            scans_count,
            flagged_count,
            last_flagged_at,
            last_reference,
        ) = row
        rollups.append(
            {
                "metric_date": metric_date,
                "guild_id": None if guild_raw in (None, 0) else int(guild_raw),
                "content_type": content,
                "scans_count": int(scans_count or 0),
                "flagged_count": int(flagged_count or 0),
                "last_flagged_at": last_flagged_at.replace(tzinfo=timezone.utc)
                if isinstance(last_flagged_at, datetime)
                else last_flagged_at,
                "last_reference": last_reference,
            }
        )
    return rollups


__all__ = [
    "insert_moderation_metric",
    "fetch_recent_metrics",
    "summarise_metrics",
    "fetch_metric_rollups",
]
