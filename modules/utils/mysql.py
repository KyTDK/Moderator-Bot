import logging
import aiomysql
import os
import json
import copy
from cryptography.fernet import Fernet
from dotenv import load_dotenv
from modules.config.settings_schema import SETTINGS_SCHEMA

load_dotenv()

MYSQL_CONFIG = {
    "host": os.getenv("MYSQL_HOST"),
    "user": os.getenv("MYSQL_USER"),
    "password": os.getenv("MYSQL_PASSWORD"),
    "db": os.getenv("MYSQL_DATABASE"), 
    "autocommit": False,
    "charset": "utf8mb4"
}

FERNET_KEY = os.getenv("FERNET_SECRET_KEY")
fernet = Fernet(FERNET_KEY)

_pool: aiomysql.Pool | None = None

async def init_pool(minsize: int = 1, maxsize: int = 10):
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

async def close_pool():
    """Gracefully close the global pool (e.g. on bot shutdown)."""
    global _pool
    if _pool is not None:
        _pool.close()
        await _pool.wait_closed()
        _pool = None

async def get_pool():
    global _pool
    if _pool is None or _pool._closed:
        await init_pool()
    return _pool

async def _connect_raw(use_database: bool = True):
    """Open a *single* connection (no pool) — used internally for bootstrap tasks."""
    cfg = MYSQL_CONFIG.copy()
    if not use_database:
        cfg.pop("db", None)
    return await aiomysql.connect(**cfg)

async def _ensure_database_exists():
    """Create the target database and tables if they are missing."""
    conn = await _connect_raw(use_database=False)
    async with conn.cursor() as cur:
        try:
            await cur.execute(f"CREATE DATABASE IF NOT EXISTS `{MYSQL_CONFIG['db']}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
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
                CREATE TABLE IF NOT EXISTS timeouts (
                    user_id BIGINT NOT NULL,
                    guild_id BIGINT NOT NULL,
                    timeout_until DATETIME NOT NULL,
                    reason VARCHAR(255),
                    source VARCHAR(32) DEFAULT 'generic',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, guild_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                """
            )
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS scam_messages (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    guild_id BIGINT NOT NULL,
                    pattern TEXT NOT NULL,
                    added_by BIGINT,
                    added_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    global_verified BOOLEAN DEFAULT FALSE,
                    INDEX (guild_id),
                    INDEX (pattern(255))
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                """
            )
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS scam_users (
                    user_id BIGINT NOT NULL,
                    guild_id BIGINT NOT NULL,
                    first_detected DATETIME DEFAULT CURRENT_TIMESTAMP,
                    matched_message_id BIGINT,
                    matched_pattern TEXT,
                    matched_url TEXT,
                    global_verified BOOLEAN DEFAULT FALSE,
                    PRIMARY KEY (user_id, guild_id),
                    INDEX (user_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                """
            )
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS scam_urls (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    guild_id BIGINT NOT NULL,
                    full_url TEXT NOT NULL,
                    added_by BIGINT,
                    added_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    global_verified BOOLEAN DEFAULT FALSE,
                    INDEX (guild_id),
                    INDEX (full_url(255))
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                """
            )
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS premium_guilds (
                    guild_id BIGINT NOT NULL,
                    buyer_id BIGINT NOT NULL,
                    subscription_id VARCHAR(64) NOT NULL,
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
                CREATE TABLE IF NOT EXISTS guilds (
                    guild_id BIGINT PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    owner_id BIGINT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY (guild_id)
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

async def get_strike_count(user_id: int, guild_id: int) -> int:
    result, _ = await execute_query(
        """
        SELECT COUNT(*)
        FROM strikes
        WHERE user_id = %s
          AND guild_id = %s
          AND (expires_at IS NULL OR expires_at > UTC_TIMESTAMP())
        """,
        (user_id, guild_id),
        fetch_one=True,
    )
    return result[0] if result else 0

async def get_strikes(user_id: int, guild_id: int):
    strikes, _ = await execute_query(
        """
        SELECT id, reason, striked_by_id, timestamp, expires_at
        FROM strikes
        WHERE user_id = %s
          AND guild_id = %s
          AND (expires_at IS NULL OR expires_at > UTC_TIMESTAMP())
        ORDER BY timestamp DESC
        """,
        (user_id, guild_id),
        fetch_all=True,
    )
    return strikes

async def get_settings(guild_id: int, settings_key: str | list[str] | None = None):
    settings_row, _ = await execute_query(
        "SELECT settings_json FROM settings WHERE guild_id = %s",
        (guild_id,),
        fetch_one=True,
    )
    raw = json.loads(settings_row[0]) if settings_row else {}

    # Normalize requested keys
    if isinstance(settings_key, str):
        requested = [settings_key]
    elif isinstance(settings_key, list):
        requested = settings_key
    else:
        # No key provided: process all schema keys + any unknown keys found in raw
        requested = list(set(SETTINGS_SCHEMA.keys()) | set(raw.keys()))

    # Only check acceleration if any requested key is accelerated-only
    needs_accel = any(getattr(SETTINGS_SCHEMA.get(k), "accelerated", False) for k in requested)
    accelerated_enabled = await is_accelerated(guild_id=guild_id) if needs_accel else None

    def process_key(key: str):
        schema = SETTINGS_SCHEMA.get(key)
        default = copy.deepcopy(getattr(schema, "default", None))
        encrypted = bool(getattr(schema, "encrypted", False))
        accelerated_only = bool(getattr(schema, "accelerated", False))

        value = raw.get(key, copy.deepcopy(default))

        # Override with default for accelerated-only settings if guild isn't accelerated
        if accelerated_only and (accelerated_enabled is False):
            value = copy.deepcopy(default)

        # Decrypt stored values when applicable
        if encrypted and value:
            value = fernet.decrypt(value.encode()).decode()

        # Type coercions / migrations
        if schema:
            if schema.type is bool and isinstance(value, str):
                value = value.lower() == "true"

            if schema.type == list[str]:
                if isinstance(value, str):
                    value = [value]
                elif not isinstance(value, list):
                    value = []
                value = [v for v in value if v != "none"]

            if key == "strike-actions":
                migrated = {}
                if isinstance(value, dict):
                    for k, v in value.items():
                        if isinstance(v, list):
                            migrated[k] = v
                        elif isinstance(v, tuple):
                            a, d = v
                            migrated[k] = [f"{a}:{d}" if d else a]
                        else:
                            migrated[k] = [str(v)]
                    value = migrated

        return value

    result = {k: process_key(k) for k in requested}

    # If a single key was requested, return just that value
    if settings_key is not None and len(requested) == 1:
        return result[requested[0]]

    return result

async def update_settings(guild_id: int, settings_key: str, settings_value):
    settings = await get_settings(guild_id)

    schema = SETTINGS_SCHEMA.get(settings_key)
    encrypt_current = schema.encrypted if schema else False

    if settings_value is None:
        changed = settings.pop(settings_key, None) is not None
    else:
        if settings_key == "strike-actions" and isinstance(settings_value, dict):
            converted = {}
            for k, v in settings_value.items():
                if isinstance(v, list):
                    converted[k] = v
                elif isinstance(v, tuple):
                    a, d = v
                    converted[k] = [f"{a}:{d}" if d else a]
                else:
                    converted[k] = [str(v)]
            settings_value = converted

        if encrypt_current:
            settings_value = fernet.encrypt(settings_value.encode()).decode()

        settings[settings_key] = settings_value
        changed = True

    settings_json = json.dumps(settings)
    await execute_query(
        """
        INSERT INTO settings (guild_id, settings_json)
        VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE settings_json = VALUES(settings_json)
        """,
        (guild_id, settings_json),
    )
    return changed

async def initialise_and_get_pool():
    """Convenience wrapper that callers can await during startup."""
    return await init_pool()

async def cleanup_orphaned_guilds(active_guild_ids):
    """Remove database records for guilds the bot is no longer in."""
    if not active_guild_ids:
        print("[cleanup] No active guild IDs provided.")
        return

    placeholders = ",".join(["%s"] * len(active_guild_ids))
    query = f"SELECT DISTINCT guild_id FROM settings WHERE guild_id NOT IN ({placeholders})"
    rows, _ = await execute_query(query, tuple(active_guild_ids), fetch_all=True)

    if not rows:
        print("[cleanup] No orphaned guilds found.")
        return

    guild_ids = [r[0] for r in rows]
    tables = [
        "settings",
        "banned_words",
        "timeouts",
        "scam_messages",
        "scam_users",
        "scam_urls",
        "strikes",
        "api_pool"
    ]

    total_deleted = 0
    for gid in guild_ids:
        print(f"[cleanup] Checking orphaned guild data for: {gid}")
        for table in tables:
            _, affected = await execute_query(f"DELETE FROM {table} WHERE guild_id = %s", (gid,))
            if affected > 0:
                print(f"[cleanup] → Deleted {affected} rows from {table} for guild {gid}")
                total_deleted += affected

    if total_deleted == 0:
        print("[cleanup] Cleanup performed, but nothing to delete.")
    else:
        print(f"[cleanup] Completed. Total rows deleted: {total_deleted}")

async def cleanup_expired_strikes():
    """
    Delete expired strikes from the database (where expires_at < now).
    """
    _, affected = await execute_query(
        """
        DELETE FROM strikes
        WHERE expires_at IS NOT NULL
          AND expires_at <= UTC_TIMESTAMP()
        """
    )
    if affected > 0:
        print(f"[strikes cleanup] Deleted {affected} expired strikes.")
    else:
        print("[strikes cleanup] No expired strikes to delete.")
    return affected

async def is_accelerated(user_id: int = None, guild_id: int = None) -> bool:
    """
    Return True if the user or guild has an active premium subscription
    that hasn't expired based on next_billing.
    """
    conditions = []
    params = []

    if guild_id:
        conditions.append("guild_id = %s")
        params.append(guild_id)
    if user_id:
        conditions.append("buyer_id = %s")
        params.append(user_id)

    if not conditions:
        return False

    where_clause = " AND ".join(conditions)

    query = f"""
        SELECT 1 FROM premium_guilds
        WHERE {where_clause}
        AND status = 'active'
        AND (next_billing IS NULL OR next_billing > UTC_TIMESTAMP())
        LIMIT 1
    """

    result, _ = await execute_query(query, tuple(params), fetch_one=True)
    return result is not None

async def add_guild(guild_id: int, name: str, owner_id: int):
    """
    Add a new guild to the database.
    """
    await execute_query(
        """
        INSERT INTO guilds (guild_id, name, owner_id)
        VALUES (%s, %s, %s)
        ON DUPLICATE KEY UPDATE name = VALUES(name), owner_id = VALUES(owner_id)
        """,
        (guild_id, name, owner_id),
    )

async def remove_guild(guild_id: int):
    """
    Remove a guild from the database.
    """
    await execute_query(
        "DELETE FROM guilds WHERE guild_id = %s",
        (guild_id,),
    )