import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Optional, Sequence

import aiomysql

from .config import MYSQL_CONFIG, MYSQL_MAX_RETRIES, MYSQL_RETRY_BACKOFF_SECONDS
from .offline_cache import ColumnDefinition, OfflineCache, OfflineQueryError


_USE_FAKE_POOL = os.getenv("MYSQL_FAKE", "").lower() in {"1", "true", "yes"}
_offline_cache = OfflineCache()
_snapshot_lock = asyncio.Lock()
_pending_flush_lock = asyncio.Lock()
_pending_write_flag = asyncio.Event()
_mysql_online = True
_LOGGER = logging.getLogger(__name__)


_RETRYABLE_MYSQL_ERROR_CODES: set[int] = {
    2003,  # Can't connect to MySQL server
    2006,  # MySQL server has gone away
    2013,  # Lost connection during query
    2055,  # Lost connection at host
}
_RETRYABLE_MYSQL_EXCEPTIONS = (
    aiomysql.OperationalError,
    aiomysql.InterfaceError,
    ConnectionError,
    OSError,
)


if _USE_FAKE_POOL:

    class _FakeCursor:
        def __init__(self) -> None:
            self.rowcount = 0

        async def execute(self, _query: str, _params: tuple | list) -> None:
            self.rowcount = 0

        async def fetchone(self) -> None:
            return None

        async def fetchall(self) -> list[Any]:
            return []

    @asynccontextmanager
    async def _fake_cursor_context() -> Any:
        cursor = _FakeCursor()
        yield cursor

    class _FakeConnection:
        def cursor(self) -> Any:
            return _fake_cursor_context()

        async def commit(self) -> None:
            return None

        async def rollback(self) -> None:
            return None

    @asynccontextmanager
    async def _fake_connection_context() -> Any:
        yield _FakeConnection()

    class _FakePool:
        def __init__(self) -> None:
            self._closed = False

        def acquire(self) -> Any:
            return _fake_connection_context()

        def close(self) -> None:
            self._closed = True

        async def wait_closed(self) -> None:
            return None

    _pool: Optional[_FakePool] = None

    async def init_pool(minsize: int = 1, maxsize: int = 10) -> _FakePool:  # noqa: ARG001
        """Create the fake connection pool used during tests."""
        global _pool
        if _pool is None:
            _pool = _FakePool()
        return _pool

    async def close_pool() -> None:
        """Reset the fake pool placeholder."""
        global _pool
        if _pool is not None:
            _pool.close()
            _pool = None

    async def get_pool() -> _FakePool:
        global _pool
        if _pool is None or getattr(_pool, "_closed", False):
            await init_pool()
        return _pool  # type: ignore[return-value]

    async def _connect_raw(use_database: bool = True) -> aiomysql.Connection:  # noqa: ARG001
        raise RuntimeError("Raw MySQL connections are not available when MYSQL_FAKE is enabled")

    async def _ensure_database_exists() -> None:
        return None

else:
    _pool: Optional[aiomysql.Pool] = None

    async def init_pool(minsize: int = 1, maxsize: int = 10) -> aiomysql.Pool:
        """Create the global aiomysql connection pool (if not already created)."""
        global _pool
        if _pool is not None:
            return _pool

        await _ensure_database_exists()

        _pool = await aiomysql.create_pool(
            minsize=minsize,
            maxsize=maxsize,
            **MYSQL_CONFIG,
        )
        await _offline_cache.ensure_started()
        await _refresh_offline_snapshot()
        _offline_cache.start_snapshot_loop(_refresh_offline_snapshot)
        return _pool

    async def close_pool() -> None:
        """Gracefully close the global pool (e.g. on bot shutdown)."""
        global _pool
        if _pool is not None:
            _pool.close()
            await _pool.wait_closed()
            _pool = None

    async def get_pool() -> aiomysql.Pool:
        global _pool
        if _pool is None or _pool._closed:
            await init_pool()
        return _pool

    async def _connect_raw(use_database: bool = True) -> aiomysql.Connection:
        """Open a *single* connection (no pool) - used internally for bootstrap tasks."""
        cfg = MYSQL_CONFIG.copy()
        if not use_database:
            cfg.pop("db", None)
        return await aiomysql.connect(**cfg)

    async def _ensure_database_exists() -> None:
        """Create the target database and tables if they are missing."""
        conn = await _connect_raw(use_database=False)
        async with conn.cursor() as cur:
            try:
                await cur.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{MYSQL_CONFIG['db']}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
                await cur.execute(f"USE `{MYSQL_CONFIG['db']}`")
                # ---------- Core tables ----------
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS strikes (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        guild_id BIGINT,
                        user_id BIGINT,
                        reason VARCHAR(255),
                        striked_by_id BIGINT,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                        expires_at DATETIME NULL DEFAULT NULL
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                    """
                )
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS settings (
                        guild_id BIGINT PRIMARY KEY,
                        settings_json JSON
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                    """
                )
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS banned_words (
                        guild_id BIGINT,
                        word VARCHAR(255),
                        PRIMARY KEY (guild_id, word)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                    """
                )
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS banned_urls (
                        guild_id BIGINT,
                        url VARCHAR(255),
                        PRIMARY KEY (guild_id, url)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                    """
                )
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS banned_guilds (
                        guild_id BIGINT PRIMARY KEY,
                        reason VARCHAR(255) NULL,
                        added_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                    """
                )
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS faq_entries (
                        guild_id BIGINT NOT NULL,
                        entry_id INT NOT NULL,
                        question TEXT NOT NULL,
                        answer TEXT NOT NULL,
                        vector_id BIGINT NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        PRIMARY KEY (guild_id, entry_id),
                        INDEX idx_faq_vector (vector_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                    """
                )
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS api_pool (
                        user_id BIGINT NOT NULL,
                        guild_id BIGINT NOT NULL,
                        api_key TEXT NOT NULL,
                        api_key_hash VARCHAR(64) NOT NULL,
                        working BOOLEAN NOT NULL DEFAULT TRUE,
                        PRIMARY KEY (user_id, api_key_hash)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                    """
                )
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS aimod_usage (
                        guild_id BIGINT NOT NULL PRIMARY KEY,
                        cycle_end DATETIME NOT NULL,
                        tokens_used BIGINT DEFAULT 0,
                        cost_usd DECIMAL(12, 6) DEFAULT 0,
                        limit_usd DECIMAL(12, 6) DEFAULT 2.00
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                    """
                )
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS vcmod_usage (
                        guild_id BIGINT NOT NULL PRIMARY KEY,
                        cycle_end DATETIME NOT NULL,
                        tokens_used BIGINT DEFAULT 0,
                        cost_usd DECIMAL(12, 6) DEFAULT 0,
                        limit_usd DECIMAL(12, 6) DEFAULT 2.00
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                    """
                )
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS premium_guilds (
                        guild_id BIGINT NOT NULL,
                        buyer_id BIGINT NOT NULL,
                        subscription_id VARCHAR(64) NOT NULL,
                        tier ENUM('accelerated', 'accelerated_pro', 'accelerated_ultra') NOT NULL DEFAULT 'accelerated',
                        status ENUM('pending', 'active', 'cancelled', 'expired') DEFAULT 'pending',
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        activated_at DATETIME NULL DEFAULT NULL,
                        next_billing DATETIME NULL DEFAULT NULL,
                        PRIMARY KEY (guild_id),
                        UNIQUE KEY subscription_id_unique (subscription_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                    """
                )
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS bot_shards (
                        shard_id INT PRIMARY KEY,
                        status VARCHAR(32) NOT NULL DEFAULT 'available',
                        claimed_by VARCHAR(128) NULL,
                        claimed_at DATETIME NULL,
                        last_heartbeat DATETIME NULL,
                        session_id VARCHAR(128) NULL,
                        resume_gateway_url TEXT NULL,
                        last_error TEXT NULL,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        INDEX idx_status (status),
                        INDEX idx_claimed_by (claimed_by)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                    """
                )
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS bot_instances (
                        instance_id VARCHAR(128) PRIMARY KEY,
                        last_seen DATETIME NOT NULL,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        INDEX idx_last_seen (last_seen)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                    """
                )
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS guilds (
                        guild_id BIGINT PRIMARY KEY,
                        name VARCHAR(255) NOT NULL,
                        owner_id BIGINT NOT NULL,
                        total_members INT UNSIGNED NOT NULL DEFAULT 0,
                        locale VARCHAR(16) NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE KEY (guild_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                    """
                )
                await cur.execute("SHOW COLUMNS FROM guilds LIKE 'total_members'")
                column = await cur.fetchone()
                if not column:
                    await cur.execute(
                        """
                        ALTER TABLE guilds
                        ADD COLUMN total_members INT UNSIGNED NOT NULL DEFAULT 0
                        AFTER owner_id
                        """
                    )
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS captcha_embeds (
                        guild_id BIGINT PRIMARY KEY,
                        channel_id BIGINT NOT NULL,
                        message_id BIGINT NOT NULL,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                    """
                )
                await conn.commit()
            finally:
                conn.close()

async def execute_query(
    query: str,
    params: tuple | list = (),
    *,
    commit: bool = True,
    fetch_one: bool = False,
    fetch_all: bool = False,
):
    await _offline_cache.ensure_started()
    normalized_params: Sequence[Any] = tuple(params or ())
    attempt = 0
    total_attempts = max(MYSQL_MAX_RETRIES, 0) + 1
    while True:
        try:
            result, affected = await _execute_mysql(
                query,
                normalized_params,
                commit=commit,
                fetch_one=fetch_one,
                fetch_all=fetch_all,
            )
        except Exception as exc:
            should_retry = attempt < MYSQL_MAX_RETRIES and _is_retryable_mysql_error(exc)
            if should_retry:
                delay = _calculate_retry_delay(attempt)
                _LOGGER.warning(
                    "MySQL query failed (attempt %s/%s); retrying in %.2fs",
                    attempt + 1,
                    total_attempts,
                    delay,
                )
                if delay > 0:
                    await asyncio.sleep(delay)
                attempt += 1
                continue

            await _handle_mysql_failure()
            return await _execute_offline(
                query,
                normalized_params,
                fetch_one=fetch_one,
                fetch_all=fetch_all,
            )

        await _handle_mysql_success(query, normalized_params)
        return result, affected

async def initialise_and_get_pool() -> Any:
    """Convenience wrapper that callers can await during startup."""
    return await init_pool()


def _calculate_retry_delay(attempt: int) -> float:
    base_delay = max(MYSQL_RETRY_BACKOFF_SECONDS, 0.0)
    if base_delay == 0:
        return 0.0
    return base_delay * (2 ** attempt)


def _is_retryable_mysql_error(exc: Exception) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None:
        obj_id = id(current)
        if obj_id in seen:
            break
        seen.add(obj_id)
        if isinstance(current, _RETRYABLE_MYSQL_EXCEPTIONS):
            return True
        code = _extract_mysql_error_code(current)
        if code is not None and code in _RETRYABLE_MYSQL_ERROR_CODES:
            return True
        current = current.__cause__ or current.__context__
    return False


def _extract_mysql_error_code(exc: BaseException) -> int | None:
    args = getattr(exc, "args", None)
    if not args:
        return None
    first = args[0]
    return first if isinstance(first, int) else None


async def refresh_offline_cache_snapshot() -> None:
    """Public helper to force-refresh the local offline cache snapshot."""
    await _refresh_offline_snapshot()


async def _execute_mysql(
    query: str,
    params: Sequence[Any],
    *,
    commit: bool,
    fetch_one: bool,
    fetch_all: bool,
) -> tuple[Any, int]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute(query, params)
                affected_rows = cur.rowcount
                result = None
                if fetch_one:
                    result = await cur.fetchone()
                elif fetch_all:
                    result = await cur.fetchall()
                if commit:
                    await conn.commit()
                return result, affected_rows
            except Exception:
                if commit:
                    await conn.rollback()
                raise


async def _execute_offline(
    query: str,
    params: Sequence[Any],
    *,
    fetch_one: bool,
    fetch_all: bool,
) -> tuple[Any, int]:
    try:
        result, affected = await _offline_cache.execute(
            query,
            params,
            fetch_one=fetch_one,
            fetch_all=fetch_all,
        )
        if OfflineCache.is_mutation(query):
            await _offline_cache.enqueue_pending_write(query, params)
            _pending_write_flag.set()
        return result, affected
    except OfflineQueryError:
        _LOGGER.exception("Offline cache could not interpret query")
    except Exception:
        _LOGGER.exception("Offline cache failed to execute query")
    return None, 0


async def _handle_mysql_success(query: str, params: Sequence[Any]) -> None:
    global _mysql_online
    if not _mysql_online:
        _LOGGER.info("MySQL connection restored; draining offline queue")
    _mysql_online = True
    if OfflineCache.is_mutation(query):
        try:
            await _offline_cache.apply_mutation(query, params)
        except OfflineQueryError:
            _LOGGER.debug("Offline mirror skipped unsupported mutation for query %s", query.strip())
        except Exception:
            _LOGGER.exception("Failed to mirror mutation into offline cache")
    await _flush_pending_writes()


async def _handle_mysql_failure() -> None:
    global _mysql_online
    if _mysql_online:
        _LOGGER.exception("MySQL query failed; switching to offline cache")
    _mysql_online = False


async def _flush_pending_writes() -> None:
    if not _pending_write_flag.is_set():
        return

    async with _pending_flush_lock:
        pending = await _offline_cache.get_pending_writes()
        if not pending:
            _pending_write_flag.clear()
            return

        for item in pending:
            try:
                await _execute_mysql(
                    item.query,
                    item.params,
                    commit=True,
                    fetch_one=False,
                    fetch_all=False,
                )
            except Exception:
                _LOGGER.exception("Failed to replay pending write %s", item.row_id)
                break
            else:
                await _offline_cache.remove_pending_write(item.row_id)

        remaining = await _offline_cache.get_pending_writes()
        if not remaining:
            _pending_write_flag.clear()


async def _refresh_offline_snapshot() -> None:
    if _USE_FAKE_POOL:
        return

    if _pool is None:
        return

    async with _snapshot_lock:
        try:
            pool = await get_pool()
        except Exception:
            return

        async with pool.acquire() as conn:
            try:
                table_names = await _fetch_mysql_table_names(conn)
            except Exception:
                _LOGGER.exception("Failed to enumerate MySQL tables for offline cache")
                return

            for table in table_names:
                try:
                    columns, primary_keys = await _describe_mysql_table(conn, table)
                    rows = await _fetch_mysql_table_rows(conn, table)
                except Exception:
                    _LOGGER.exception("Failed to snapshot table %s", table)
                    continue

                try:
                    await _offline_cache.sync_schema(table, columns, primary_keys)
                    await _offline_cache.replace_table(table, rows)
                except Exception:
                    _LOGGER.exception("Failed to refresh offline data for table %s", table)


async def _fetch_mysql_table_names(conn: aiomysql.Connection) -> list[str]:
    async with conn.cursor() as cur:
        await cur.execute("SHOW TABLES")
        rows = await cur.fetchall()
    tables: list[str] = []
    for row in rows:
        if isinstance(row, dict):
            value = next(iter(row.values()), None)
        else:
            value = row[0] if row else None
        if value:
            tables.append(str(value))
    return tables


async def _describe_mysql_table(
    conn: aiomysql.Connection,
    table: str,
) -> tuple[list[ColumnDefinition], list[str]]:
    async with conn.cursor() as cur:
        await cur.execute(f"DESCRIBE `{table}`")
        rows = await cur.fetchall()

    columns: list[ColumnDefinition] = []
    primary_keys: list[str] = []
    for row in rows:
        field = row[0]
        type_spec = row[1] if len(row) > 1 else "text"
        key_flag = row[3] if len(row) > 3 else ""
        column_name = str(field)
        columns.append(
            ColumnDefinition(
                name=column_name,
                affinity=_mysql_type_to_affinity(str(type_spec)),
            )
        )
        if str(key_flag).upper() == "PRI":
            primary_keys.append(column_name)
    return columns, primary_keys


async def _fetch_mysql_table_rows(
    conn: aiomysql.Connection,
    table: str,
) -> list[dict[str, Any]]:
    async with conn.cursor(aiomysql.DictCursor) as cur:
        await cur.execute(f"SELECT * FROM `{table}`")
        rows = await cur.fetchall()
    return rows or []


def _mysql_type_to_affinity(type_spec: str) -> str:
    base = type_spec.split("(", 1)[0].strip().lower()
    if base in {
        "tinyint",
        "smallint",
        "mediumint",
        "int",
        "integer",
        "bigint",
        "bit",
        "year",
        "boolean",
    }:
        return "INTEGER"
    if base in {"decimal", "numeric", "float", "double", "real"}:
        return "REAL"
    if base in {
        "blob",
        "tinyblob",
        "mediumblob",
        "longblob",
        "binary",
        "varbinary",
        "bytea",
    }:
        return "BLOB"
    return "TEXT"
