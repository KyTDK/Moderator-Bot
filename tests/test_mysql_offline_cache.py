import asyncio
import base64
import os
from decimal import Decimal

os.environ.setdefault("FERNET_SECRET_KEY", base64.urlsafe_b64encode(b"0" * 32).decode())

from modules.utils.mysql.offline_cache import ColumnDefinition, OfflineCache


def test_offline_cache_insert_and_query(tmp_path):
    async def _run():
        cache_path = tmp_path / "mirror.sqlite3"
        cache = OfflineCache(db_path=str(cache_path), snapshot_interval_seconds=1_000)
        await cache.ensure_started()
        await cache.sync_schema(
            "guilds",
            [
                ColumnDefinition("guild_id", "INTEGER"),
                ColumnDefinition("name", "TEXT"),
                ColumnDefinition("owner_id", "INTEGER"),
                ColumnDefinition("locale", "TEXT"),
                ColumnDefinition("total_members", "INTEGER"),
            ],
            ["guild_id"],
        )

        await cache.apply_mutation(
            """
            INSERT INTO guilds (guild_id, name, owner_id, locale, total_members)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                name = VALUES(name),
                locale = VALUES(locale)
            """,
            (123, "Test Guild", 987, "en-US", 42),
        )

        row, _ = await cache.execute(
            "SELECT name, locale FROM guilds WHERE guild_id = %s",
            (123,),
            fetch_one=True,
        )
        assert row == ("Test Guild", "en-US")
        await cache.close()

    asyncio.run(_run())


def test_translate_on_duplicate(tmp_path):
    async def _run():
        cache_path = tmp_path / "mirror.sqlite3"
        cache = OfflineCache(db_path=str(cache_path), snapshot_interval_seconds=1_000)
        await cache.sync_schema(
            "settings",
            [
                ColumnDefinition("guild_id", "INTEGER"),
                ColumnDefinition("settings_json", "TEXT"),
            ],
            ["guild_id"],
        )
        sql = cache._translate(
            """
            INSERT INTO settings (guild_id, settings_json)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE settings_json = VALUES(settings_json)
            """
        )
        normalized = " ".join(sql.split())
        assert "ON CONFLICT(\"guild_id\")" in normalized
        assert "settings_json = excluded.settings_json" in normalized
        await cache.close()

    asyncio.run(_run())


def test_pending_write_queue(tmp_path):
    async def _run():
        cache_path = tmp_path / "mirror.sqlite3"
        cache = OfflineCache(db_path=str(cache_path), snapshot_interval_seconds=1_000)
        await cache.ensure_started()

        await cache.enqueue_pending_write(
            "DELETE FROM guilds WHERE guild_id = %s",
            (555,),
        )

        pending = await cache.get_pending_writes()
        assert len(pending) == 1
        assert pending[0].query.startswith("DELETE FROM guilds")

        await cache.remove_pending_write(pending[0].row_id)
        remaining = await cache.get_pending_writes()
        assert remaining == []
        await cache.close()

    asyncio.run(_run())


def test_replace_table_handles_decimal(tmp_path):
    async def _run():
        cache_path = tmp_path / "mirror.sqlite3"
        cache = OfflineCache(db_path=str(cache_path), snapshot_interval_seconds=1_000)
        await cache.ensure_started()
        await cache.sync_schema(
            "usage",
            [
                ColumnDefinition("guild_id", "INTEGER"),
                ColumnDefinition("cost_usd", "REAL"),
            ],
            ["guild_id"],
        )

        await cache.replace_table(
            "usage",
            [
                {"guild_id": 1, "cost_usd": Decimal("1.234567")},
            ],
        )

        row, _ = await cache.execute(
            "SELECT cost_usd FROM usage WHERE guild_id = %s",
            (1,),
            fetch_one=True,
        )
        assert row[0] == float(Decimal("1.234567"))
        await cache.close()

    asyncio.run(_run())
