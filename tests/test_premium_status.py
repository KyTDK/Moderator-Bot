import asyncio
import os
import pytest
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

cryptography_stub = types.ModuleType("cryptography")
cryptography_fernet_stub = types.ModuleType("cryptography.fernet")
aiomysql_stub = types.ModuleType("aiomysql")
discord_stub = types.ModuleType("discord")


class _DummyConnection:
    pass


async def _dummy_create_pool(*args, **kwargs):
    raise RuntimeError("create_pool should not be called in tests")


class _DummyFernet:
    def __init__(self, key: bytes | str):
        self.key = key

    def encrypt(self, value: bytes):
        return value

    def decrypt(self, value: bytes):
        return value


cryptography_fernet_stub.Fernet = _DummyFernet
sys.modules.setdefault("cryptography", cryptography_stub)
sys.modules.setdefault("cryptography.fernet", cryptography_fernet_stub)
sys.modules.setdefault("aiomysql", aiomysql_stub)
sys.modules.setdefault("discord", discord_stub)
aiomysql_stub.Connection = _DummyConnection
aiomysql_stub.cursors = types.SimpleNamespace(DictCursor=object())
aiomysql_stub.create_pool = _dummy_create_pool
discord_stub.Locale = type("Locale", (), {"__init__": lambda self, value: setattr(self, "value", value)})
discord_stub.TextChannel = type("TextChannel", (), {})
discord_stub.VoiceChannel = type("VoiceChannel", (), {})
discord_stub.Role = type("Role", (), {})

os.environ.setdefault("FERNET_SECRET_KEY", "dummy-key")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules.config.premium_plans import PLAN_CORE
from modules.utils.mysql import premium as premium_module


def test_get_premium_status_treats_cancelled_with_future_iso_z_as_active(monkeypatch):
    guild_id = 12345

    async def fake_execute_query(_query, _params, *, fetch_one=False, fetch_all=False):
        assert fetch_one
        # Simulate a cancelled subscription with an ISO8601 timestamp ending in Z
        return (("cancelled", "2099-05-01T12:30:00Z", "core"), None)

    monkeypatch.setattr(
        premium_module,
        "execute_query",
        fake_execute_query,
        raising=False,
    )

    async def _run():
        return await premium_module.get_premium_status(guild_id)

    status = asyncio.run(_run())
    assert status is not None
    assert status["status"] == "cancelled"
    assert status["tier"] == "core"
    assert status["is_active"] is True
    assert isinstance(status["next_billing"], datetime)
    assert status["next_billing"].tzinfo == timezone.utc


def test_resolve_guild_plan_handles_cancelled_future_iso_z(monkeypatch):
    guild_id = 67890

    async def fake_execute_query(_query, _params, *, fetch_one=False, fetch_all=False):
        assert fetch_one
        return (("cancelled", "2099-12-31T23:59:59Z", "core"), None)

    monkeypatch.setattr(
        premium_module,
        "execute_query",
        fake_execute_query,
        raising=False,
    )

    async def _run():
        return await premium_module.resolve_guild_plan(guild_id)

    plan = asyncio.run(_run())
    assert plan == PLAN_CORE


def test_is_accelerated_true_for_cancelled_with_future_iso_z(monkeypatch):
    guild_id = 24680

    async def fake_execute_query(_query, _params, *, fetch_one=False, fetch_all=False):
        assert fetch_one
        return (("cancelled", "2099-08-15T18:45:00Z"), None)

    monkeypatch.setattr(
        premium_module,
        "execute_query",
        fake_execute_query,
        raising=False,
    )

    async def _run():
        return await premium_module.is_accelerated(guild_id=guild_id)

    assert asyncio.run(_run()) is True


def test_is_accelerated_false_when_expired(monkeypatch):
    guild_id = 13579

    async def fake_execute_query(_query, _params, *, fetch_one=False, fetch_all=False):
        assert fetch_one
        return (("cancelled", "2000-01-01T00:00:00Z"), None)

    monkeypatch.setattr(
        premium_module,
        "execute_query",
        fake_execute_query,
        raising=False,
    )

    async def _run():
        return await premium_module.is_accelerated(guild_id=guild_id)

    assert asyncio.run(_run()) is False
