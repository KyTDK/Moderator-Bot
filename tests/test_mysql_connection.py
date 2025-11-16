import asyncio
import base64
import os
import sys
import types
from pathlib import Path

import pytest


os.environ.setdefault("FERNET_SECRET_KEY", base64.urlsafe_b64encode(b"0" * 32).decode())


cryptography_stub = types.ModuleType("cryptography")
cryptography_fernet_stub = types.ModuleType("cryptography.fernet")


class _DummyFernet:
    def __init__(self, key: bytes | str):  # noqa: D401, ARG002 - compatibility stub
        self.key = key

    def encrypt(self, value: bytes):  # pragma: no cover - helper not used directly
        return value

    def decrypt(self, value: bytes):  # pragma: no cover - helper not used directly
        return value


cryptography_fernet_stub.Fernet = _DummyFernet
sys.modules.setdefault("cryptography", cryptography_stub)
sys.modules.setdefault("cryptography.fernet", cryptography_fernet_stub)


aiomysql_stub = types.ModuleType("aiomysql")


class _DummyMySQLError(Exception):
    pass


class OperationalError(_DummyMySQLError):
    pass


class InterfaceError(_DummyMySQLError):
    pass


async def _unexpected_async_call(*args, **kwargs):  # pragma: no cover - safeguard
    raise AssertionError("aiomysql network calls should not occur in unit tests")


aiomysql_stub.Error = _DummyMySQLError
aiomysql_stub.OperationalError = OperationalError
aiomysql_stub.InterfaceError = InterfaceError
aiomysql_stub.Connection = type("Connection", (), {})
aiomysql_stub.Pool = type("Pool", (), {})
aiomysql_stub.cursors = types.SimpleNamespace(DictCursor=object())
aiomysql_stub.create_pool = _unexpected_async_call
aiomysql_stub.connect = _unexpected_async_call
sys.modules.setdefault("aiomysql", aiomysql_stub)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from modules.utils.mysql import connection as mysql_connection


@pytest.fixture(autouse=True)
def _stub_offline_cache(monkeypatch):
    async def _ensure_started():
        return None

    monkeypatch.setattr(mysql_connection._offline_cache, "ensure_started", _ensure_started)


def _run(coro):
    return asyncio.run(coro)


def test_execute_query_retries_before_offline(monkeypatch):
    monkeypatch.setattr(mysql_connection, "MYSQL_MAX_RETRIES", 2, raising=False)
    monkeypatch.setattr(mysql_connection, "MYSQL_RETRY_BACKOFF_SECONDS", 0.1, raising=False)

    attempts: list[int] = []

    async def _failing_execute_mysql(*args, **kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise OperationalError(2003, "down")
        return (("success",), 1)

    offline_called = False

    async def _execute_offline(*args, **kwargs):
        nonlocal offline_called
        offline_called = True
        return (("offline",), 0)

    sleep_calls: list[float] = []

    async def _fake_sleep(delay):
        sleep_calls.append(delay)

    monkeypatch.setattr(mysql_connection, "_execute_mysql", _failing_execute_mysql, raising=False)
    monkeypatch.setattr(mysql_connection, "_execute_offline", _execute_offline, raising=False)
    monkeypatch.setattr(mysql_connection.asyncio, "sleep", _fake_sleep)

    result = _run(mysql_connection.execute_query("SELECT 1", ()))

    assert result == (("success",), 1)
    assert len(attempts) == 3  # initial try + 2 retries
    assert offline_called is False
    assert sleep_calls == [0.1, 0.2]


def test_execute_query_falls_back_after_retry_limit(monkeypatch):
    monkeypatch.setattr(mysql_connection, "MYSQL_MAX_RETRIES", 2, raising=False)
    monkeypatch.setattr(mysql_connection, "MYSQL_RETRY_BACKOFF_SECONDS", 0.05, raising=False)

    attempts: list[int] = []

    async def _always_fail(*args, **kwargs):
        attempts.append(1)
        raise OperationalError(2003, "down")

    offline_calls: list[int] = []

    async def _execute_offline(*args, **kwargs):
        offline_calls.append(1)
        return (("offline",), 0)

    sleep_calls: list[float] = []

    async def _fake_sleep(delay):
        sleep_calls.append(delay)

    monkeypatch.setattr(mysql_connection, "_execute_mysql", _always_fail, raising=False)
    monkeypatch.setattr(mysql_connection, "_execute_offline", _execute_offline, raising=False)
    monkeypatch.setattr(mysql_connection.asyncio, "sleep", _fake_sleep)

    result = _run(mysql_connection.execute_query("SELECT 1", ()))

    assert result == (("offline",), 0)
    assert len(attempts) == 3  # initial try + retries
    assert len(offline_calls) == 1
    assert sleep_calls == [0.05, 0.1]


def test_execute_query_non_retryable_error(monkeypatch):
    monkeypatch.setattr(mysql_connection, "MYSQL_MAX_RETRIES", 5, raising=False)
    monkeypatch.setattr(mysql_connection, "MYSQL_RETRY_BACKOFF_SECONDS", 0.2, raising=False)

    attempts: list[int] = []

    async def _fail_non_retryable(*args, **kwargs):
        attempts.append(1)
        raise RuntimeError("syntax error")

    offline_calls: list[int] = []

    async def _execute_offline(*args, **kwargs):
        offline_calls.append(1)
        return (("offline",), 0)

    sleep_calls: list[float] = []

    async def _fake_sleep(delay):
        sleep_calls.append(delay)

    monkeypatch.setattr(mysql_connection, "_execute_mysql", _fail_non_retryable, raising=False)
    monkeypatch.setattr(mysql_connection, "_execute_offline", _execute_offline, raising=False)
    monkeypatch.setattr(mysql_connection.asyncio, "sleep", _fake_sleep)

    result = _run(mysql_connection.execute_query("SELECT 1", ()))

    assert result == (("offline",), 0)
    assert len(attempts) == 1
    assert len(offline_calls) == 1
    assert sleep_calls == []
