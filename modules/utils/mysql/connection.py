import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Optional

import aiomysql

from .config import MYSQL_CONFIG


_USE_FAKE_POOL = os.getenv("MYSQL_FAKE", "").lower() in {"1", "true", "yes"}


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
                logging.exception("Error executing query")
                if commit:
                    await conn.rollback()
                return None, 0

async def initialise_and_get_pool() -> Any:
    """Convenience wrapper that callers can await during startup."""
    return await init_pool()
