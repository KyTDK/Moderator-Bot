from __future__ import annotations

import asyncio
import json
import logging
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
]

_LOGGER = logging.getLogger(__name__)


class OfflineQueryError(RuntimeError):
    """Raised when a SQL statement cannot be adapted for offline execution."""


@dataclass(slots=True)
class PendingWrite:
    row_id: int
    query: str
    params: tuple[Any, ...]


def _normalize_value(value: Any) -> Any:
    """Convert complex Python values into SQLite-friendly representations."""
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


_PLACEHOLDER_TOKEN = "%s"


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
        raise OfflineQueryError("No conflict target registered for ON DUPLICATE KEY statement")

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
    conflict_clause = ", ".join(conflict_target)
    return f"{before} ON CONFLICT({conflict_clause}) DO UPDATE SET {rewritten_after}"


_TABLE_SCHEMAS: dict[str, str] = {
    "strikes": """
        CREATE TABLE IF NOT EXISTS strikes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            user_id INTEGER,
            reason TEXT,
            striked_by_id INTEGER,
            timestamp TEXT,
            expires_at TEXT
        )
    """,
    "settings": """
        CREATE TABLE IF NOT EXISTS settings (
            guild_id INTEGER PRIMARY KEY,
            settings_json TEXT
        )
    """,
    "banned_words": """
        CREATE TABLE IF NOT EXISTS banned_words (
            guild_id INTEGER NOT NULL,
            word TEXT NOT NULL,
            PRIMARY KEY (guild_id, word)
        )
    """,
    "banned_urls": """
        CREATE TABLE IF NOT EXISTS banned_urls (
            guild_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            PRIMARY KEY (guild_id, url)
        )
    """,
    "banned_guilds": """
        CREATE TABLE IF NOT EXISTS banned_guilds (
            guild_id INTEGER PRIMARY KEY,
            reason TEXT,
            added_at TEXT
        )
    """,
    "faq_entries": """
        CREATE TABLE IF NOT EXISTS faq_entries (
            guild_id INTEGER NOT NULL,
            entry_id INTEGER NOT NULL,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            vector_id INTEGER,
            created_at TEXT,
            updated_at TEXT,
            PRIMARY KEY (guild_id, entry_id)
        )
    """,
    "api_pool": """
        CREATE TABLE IF NOT EXISTS api_pool (
            user_id INTEGER NOT NULL,
            guild_id INTEGER NOT NULL,
            api_key TEXT NOT NULL,
            api_key_hash TEXT NOT NULL,
            working INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (user_id, api_key_hash)
        )
    """,
    "aimod_usage": """
        CREATE TABLE IF NOT EXISTS aimod_usage (
            guild_id INTEGER PRIMARY KEY,
            cycle_end TEXT NOT NULL,
            tokens_used INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0,
            limit_usd REAL DEFAULT 0
        )
    """,
    "vcmod_usage": """
        CREATE TABLE IF NOT EXISTS vcmod_usage (
            guild_id INTEGER PRIMARY KEY,
            cycle_end TEXT NOT NULL,
            tokens_used INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0,
            limit_usd REAL DEFAULT 0
        )
    """,
    "premium_guilds": """
        CREATE TABLE IF NOT EXISTS premium_guilds (
            guild_id INTEGER PRIMARY KEY,
            buyer_id INTEGER NOT NULL,
            subscription_id TEXT NOT NULL UNIQUE,
            tier TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT,
            activated_at TEXT,
            next_billing TEXT
        )
    """,
    "bot_shards": """
        CREATE TABLE IF NOT EXISTS bot_shards (
            shard_id INTEGER PRIMARY KEY,
            status TEXT,
            claimed_by TEXT,
            claimed_at TEXT,
            last_heartbeat TEXT,
            session_id TEXT,
            resume_gateway_url TEXT,
            last_error TEXT,
            updated_at TEXT
        )
    """,
    "bot_instances": """
        CREATE TABLE IF NOT EXISTS bot_instances (
            instance_id TEXT PRIMARY KEY,
            last_seen TEXT NOT NULL,
            updated_at TEXT
        )
    """,
    "guilds": """
        CREATE TABLE IF NOT EXISTS guilds (
            guild_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            owner_id INTEGER NOT NULL,
            total_members INTEGER NOT NULL DEFAULT 0,
            locale TEXT,
            created_at TEXT
        )
    """,
    "captcha_embeds": """
        CREATE TABLE IF NOT EXISTS captcha_embeds (
            guild_id INTEGER PRIMARY KEY,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            updated_at TEXT
        )
    """,
}

PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "strikes": ("id",),
    "settings": ("guild_id",),
    "banned_words": ("guild_id", "word"),
    "banned_urls": ("guild_id", "url"),
    "banned_guilds": ("guild_id",),
    "faq_entries": ("guild_id", "entry_id"),
    "api_pool": ("user_id", "api_key_hash"),
    "aimod_usage": ("guild_id",),
    "vcmod_usage": ("guild_id",),
    "premium_guilds": ("guild_id",),
    "bot_shards": ("shard_id",),
    "bot_instances": ("instance_id",),
    "guilds": ("guild_id",),
    "captcha_embeds": ("guild_id",),
}


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

    @property
    def managed_tables(self) -> Sequence[str]:
        return tuple(_TABLE_SCHEMAS.keys())

    async def ensure_started(self) -> aiosqlite.Connection:
        if self._conn is not None:
            return self._conn

        async with self._conn_lock:
            if self._conn is not None:
                return self._conn

            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = await aiosqlite.connect(self._db_path)
            await conn.execute("PRAGMA journal_mode=WAL;")
            await conn.execute("PRAGMA synchronous=NORMAL;")
            await conn.execute("PRAGMA foreign_keys=OFF;")
            for statement in _TABLE_SCHEMAS.values():
                await conn.execute(_strip_backticks(statement))
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
        table = table.strip()
        await conn.execute(f"DELETE FROM {table}")

        materialized_rows = list(rows)
        if not materialized_rows:
            await conn.commit()
            return

        columns = list(materialized_rows[0].keys())
        placeholders = ", ".join(["?"] * len(columns))
        column_clause = ", ".join(columns)
        payload = [
            tuple(_normalize_value(row.get(column)) for column in columns)
            for row in materialized_rows
        ]
        await conn.executemany(
            f"INSERT INTO {table} ({column_clause}) VALUES ({placeholders})",
            payload,
        )
        await conn.commit()

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
            conflict_target = PRIMARY_KEYS.get(table_name or "")
            sql = _convert_on_duplicate(sql, conflict_target or ())
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
