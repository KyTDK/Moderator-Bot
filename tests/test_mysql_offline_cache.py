import asyncio
import base64
import os

os.environ.setdefault("FERNET_SECRET_KEY", base64.urlsafe_b64encode(b"0" * 32).decode())

from modules.utils.mysql.offline_cache import OfflineCache


def test_offline_cache_insert_and_query(tmp_path):
    async def _run():
        cache_path = tmp_path / "mirror.sqlite3"
        cache = OfflineCache(db_path=str(cache_path), snapshot_interval_seconds=1_000)
        await cache.ensure_started()

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

    asyncio.run(_run())


def test_translate_on_duplicate(tmp_path):
    cache_path = tmp_path / "mirror.sqlite3"
    cache = OfflineCache(db_path=str(cache_path), snapshot_interval_seconds=1_000)
    sql = cache._translate(
        """
        INSERT INTO settings (guild_id, settings_json)
        VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE settings_json = VALUES(settings_json)
        """
    )
    normalized = " ".join(sql.split())
    assert "ON CONFLICT(guild_id)" in normalized
    assert "settings_json = excluded.settings_json" in normalized


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

    asyncio.run(_run())
