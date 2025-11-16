from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Sequence

import aiosqlite

__all__ = [
    "OfflineCache",
    "OfflineQueryError",
    "PendingWrite",
    "ColumnDefinition",
]

_LOGGER = logging.getLogger(__name__)


class OfflineQueryError(RuntimeError):
    """Raised when a SQL statement cannot be adapted for offline execution."""


@dataclass(slots=True)
class PendingWrite:
    row_id: int
    query: str
    params: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class ColumnDefinition:
    name: str
    affinity: str


@dataclass(frozen=True, slots=True)
class _SchemaState:
    columns: tuple[ColumnDefinition, ...]
    primary_keys: tuple[str, ...]


def _normalize_value(value: Any) -> Any:
    """Convert complex Python values into SQLite-friendly representations."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        adjusted = value
        if value.tzinfo:
            adjusted = value.astimezone(timezone.utc)
        return adjusted.replace(tzinfo=None).isoformat(sep=" ")
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return value


def _strip_backticks(sql: str) -> str:
    return sql.replace("`", "")


def _quote_identifier(identifier: str) -> str:
    escaped = identifier.replace('"', '""')
    return f'"{escaped}"'


def _convert_placeholders(sql: str) -> str:
    """Translate MySQL-style placeholders to SQLite's question marks."""
    result: list[str] = []
    i = 0
    while i < len(sql):
        if sql[i] == "%" and i + 1 < len(sql) and sql[i + 1] == "s":
            result.append("?")
            i += 2
        else:
            result.append(sql[i])
            i += 1
    return "".join(result)


def _convert_on_duplicate(sql: str, conflict_target: Sequence[str]) -> str:
    upper_sql = sql.upper()
    marker = "ON DUPLICATE KEY UPDATE"
    idx = upper_sql.find(marker)
    if idx == -1:
        return sql

    before = sql[:idx].rstrip()
    after = sql[idx + len(marker) :].strip()
    if not conflict_target:
        raise OfflineQueryError("Conflict target required for ON DUPLICATE KEY translation")

    # Replace VALUES(column) with excluded.column for SQLite UPSERT.
    def _values_to_excluded(segment: str) -> str:
        parts: list[str] = []
        i = 0
        while i < len(segment):
            if segment[i : i + 7].upper() == "VALUES(":
                i += 7
                column_chars: list[str] = []
                depth = 1
                while i < len(segment) and depth > 0:
                    char = segment[i]
                    if char == "(":
                        depth += 1
                    elif char == ")":
                        depth -= 1
                        if depth == 0:
                            i += 1
                            break
                    column_chars.append(char)
                    i += 1
                column = "".join(column_chars).strip()
                parts.append(f"excluded.{column}")
            else:
                parts.append(segment[i])
                i += 1
        return "".join(parts)

    rewritten_after = _values_to_excluded(after)
    conflict_clause = ", ".join(_quote_identifier(column) for column in conflict_target)
    return f"{before} ON CONFLICT({conflict_clause}) DO UPDATE SET {rewritten_after}"


class OfflineCache:
    """SQLite-backed mirror of the MySQL schema for offline continuity."""

    def __init__(
        self,
        *,
        db_path: str | Path = "data/mysql_cache.sqlite3",
        snapshot_interval_seconds: int = 120,
    ) -> None:
        self._db_path = Path(db_path)
        self._snapshot_interval = snapshot_interval_seconds
        self._conn: aiosqlite.Connection | None = None
        self._conn_lock = asyncio.Lock()
        self._snapshot_task: asyncio.Task[None] | None = None
        self._snapshot_callback: Callable[[], Awaitable[None]] | None = None
        self._schema_cache: dict[str, _SchemaState] = {}

    async def ensure_started(self) -> aiosqlite.Connection:
        if self._conn is not None:
            return self._conn

        async with self._conn_lock:
            if self._conn is not None:
                return self._conn

            self._db_path.parent.mkdir(parents=True, exist_ok=True)

            # aiosqlite.Connection is a threading.Thread subclass. If the
            # connection is never explicitly closed the associated worker
            # thread keeps the interpreter alive forever (observed under
            # pytest where the suite completed but the process never exited).
            # Mark the thread as daemon before awaiting the connection so the
            # process can terminate even if a caller forgets to close us.
            conn_task = aiosqlite.connect(self._db_path)
            conn_task.daemon = True
            conn = await conn_task
            await conn.execute("PRAGMA journal_mode=WAL;")
            await conn.execute("PRAGMA synchronous=NORMAL;")
            await conn.execute("PRAGMA foreign_keys=OFF;")
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_writes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT NOT NULL,
                    params_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await conn.commit()
            self._conn = conn

        return self._conn

    def start_snapshot_loop(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Begin periodically refreshing the mirror by calling *callback*."""
        self._snapshot_callback = callback
        if self._snapshot_task and not self._snapshot_task.done():
            return
        loop = asyncio.get_running_loop()
        self._snapshot_task = loop.create_task(self._snapshot_worker())

    async def _snapshot_worker(self) -> None:
        while True:
            if self._snapshot_callback is None:
                return
            try:
                await self._snapshot_callback()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - best-effort logging
                _LOGGER.exception("Failed to refresh offline MySQL snapshot")
            await asyncio.sleep(self._snapshot_interval)

    async def close(self) -> None:
        """Dispose of the SQLite connection and any snapshot workers."""
        if self._snapshot_task:
            self._snapshot_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._snapshot_task
            self._snapshot_task = None

        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            self._schema_cache.clear()

    async def execute(
        self,
        query: str,
        params: Sequence[Any] | None = None,
        *,
        fetch_one: bool = False,
        fetch_all: bool = False,
    ) -> tuple[Any, int]:
        """Execute a translated SQL statement against the local mirror."""
        conn = await self.ensure_started()
        sql = self._translate(query)
        normalized_params = tuple(_normalize_value(p) for p in (params or ()))
        cursor = await conn.execute(sql, normalized_params)
        try:
            if fetch_all:
                result = await cursor.fetchall()
            elif fetch_one:
                result = await cursor.fetchone()
            else:
                result = None
            rowcount = cursor.rowcount or 0
        finally:
            await cursor.close()
        await conn.commit()
        return result, rowcount

    async def apply_mutation(self, query: str, params: Sequence[Any] | None = None) -> None:
        """Replay a mutating statement to keep the mirror in sync."""
        await self.execute(query, params or ())

    async def replace_table(self, table: str, rows: Iterable[dict[str, Any]]) -> None:
        """Bulk-replace table contents with authoritative rows from MySQL."""
        conn = await self.ensure_started()
        schema = self._schema_cache.get(table)
        if schema is None:
            raise OfflineQueryError(f"Schema is not registered for table '{table}'")

        column_names = [column.name for column in schema.columns]
        if not column_names:
            return

        quoted_table = _quote_identifier(table)
        await conn.execute(f"DELETE FROM {quoted_table}")

        materialized_rows = list(rows)
        if materialized_rows:
            placeholders = ", ".join(["?"] * len(column_names))
            column_clause = ", ".join(_quote_identifier(column) for column in column_names)
            payload = [
                tuple(_normalize_value(row.get(column)) for column in column_names)
                for row in materialized_rows
            ]
            await conn.executemany(
                f"INSERT INTO {quoted_table} ({column_clause}) VALUES ({placeholders})",
                payload,
            )
        await conn.commit()

    async def sync_schema(
        self,
        table: str,
        columns: Sequence[ColumnDefinition],
        primary_keys: Sequence[str],
    ) -> None:
        """Ensure the SQLite mirror has an up-to-date table definition."""
        normalized_columns = tuple(columns)
        normalized_primary_keys = tuple(primary_keys)
        state = _SchemaState(normalized_columns, normalized_primary_keys)
        if self._schema_cache.get(table) == state:
            return

        conn = await self.ensure_started()
        quoted_table = _quote_identifier(table)
        await conn.execute(f"DROP TABLE IF EXISTS {quoted_table}")

        column_clause = ", ".join(
            f"{_quote_identifier(column.name)} {column.affinity}"
            for column in normalized_columns
        )
        pk_clause = (
            f", PRIMARY KEY ({', '.join(_quote_identifier(pk) for pk in normalized_primary_keys)})"
            if normalized_primary_keys
            else ""
        )
        await conn.execute(f"CREATE TABLE {quoted_table} ({column_clause}{pk_clause})")
        await conn.commit()
        self._schema_cache[table] = state

    async def enqueue_pending_write(self, query: str, params: Sequence[Any]) -> None:
        conn = await self.ensure_started()
        params_json = json.dumps([_normalize_value(value) for value in params])
        await conn.execute(
            "INSERT INTO pending_writes (query, params_json) VALUES (?, ?)",
            (query, params_json),
        )
        await conn.commit()

    async def get_pending_writes(self) -> list[PendingWrite]:
        conn = await self.ensure_started()
        cursor = await conn.execute(
            "SELECT id, query, params_json FROM pending_writes ORDER BY id ASC"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        pending: list[PendingWrite] = []
        for row_id, query, params_json in rows:
            try:
                params = tuple(json.loads(params_json))
            except json.JSONDecodeError:
                params = ()
            pending.append(PendingWrite(row_id=row_id, query=query, params=params))
        return pending

    async def remove_pending_write(self, row_id: int) -> None:
        conn = await self.ensure_started()
        await conn.execute("DELETE FROM pending_writes WHERE id = ?", (row_id,))
        await conn.commit()

    @staticmethod
    def is_mutation(query: str) -> bool:
        prefix = _strip_backticks(query).lstrip().lower()
        return prefix.startswith(("insert", "update", "delete", "replace"))

    def _translate(self, query: str) -> str:
        sql = _strip_backticks(query.strip().rstrip(";"))
        sql = sql.replace("UTC_TIMESTAMP()", "CURRENT_TIMESTAMP")
        sql = sql.replace("utc_timestamp()", "CURRENT_TIMESTAMP")
        sql = _convert_placeholders(sql)

        upper_sql = sql.upper()
        if "ON DUPLICATE KEY UPDATE" in upper_sql:
            table_name = self._extract_table_name(sql)
            state = self._schema_cache.get(table_name or "")
            conflict_target: Sequence[str] = state.primary_keys if state else ()
            sql = _convert_on_duplicate(sql, conflict_target)
        return sql

    @staticmethod
    def _extract_table_name(sql: str) -> str | None:
        lowered = sql.lower()
        marker = "insert into "
        idx = lowered.find(marker)
        if idx == -1:
            return None
        rest = lowered[idx + len(marker) :].lstrip()
        terminators = (" ", "(", "\n")
        for terminator in terminators:
            term_idx = rest.find(terminator)
            if term_idx != -1:
                return rest[:term_idx]
        return rest or None
