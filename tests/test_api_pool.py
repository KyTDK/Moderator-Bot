import os
import sys
import time
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("FERNET_SECRET_KEY", Fernet.generate_key().decode())

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

from modules.utils import api


@pytest.fixture(autouse=True)
def _stub_safe_get_user(monkeypatch):
    async def _safe_get_user_stub(*_args, **_kwargs):
        return None

    monkeypatch.setattr(api, "safe_get_user", _safe_get_user_stub, raising=False)


@pytest.fixture(autouse=True)
def _prevent_unpatched_execute_query(monkeypatch):
    async def _unpatched_execute_query(*_args, **_kwargs):
        raise AssertionError("execute_query should be monkeypatched in tests")

    monkeypatch.setattr(api.mysql, "execute_query", _unpatched_execute_query, raising=False)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_set_api_key_not_working_moves_key_out_of_working_pool(monkeypatch):
    key = "encrypted-key"

    monkeypatch.setattr(api, "_working_keys", [key])
    monkeypatch.setattr(api, "_non_working_keys", [], raising=False)
    monkeypatch.setattr(api, "_quarantine", {}, raising=False)
    monkeypatch.setattr(api, "_rate_limit_state", {}, raising=False)

    async def fake_execute_query(_query, _params=None, *, fetch_one=False, fetch_all=False):
        return None, 1

    monkeypatch.setattr(api.mysql, "execute_query", fake_execute_query)

    await api.set_api_key_not_working(key, bot=None)

    assert key not in api._working_keys
    assert key in api._non_working_keys
    assert key in api._quarantine
    assert api._quarantine[key] > time.monotonic()
    assert key not in api._rate_limit_state


@pytest.mark.anyio
async def test_mark_api_key_rate_limited_returns_cooldown(monkeypatch):
    key = "encrypted-key"
    monkeypatch.setattr(api, "_working_keys", [key], raising=False)
    monkeypatch.setattr(api, "_non_working_keys", [], raising=False)
    monkeypatch.setattr(api, "_quarantine", {}, raising=False)
    monkeypatch.setattr(api, "_rate_limit_state", {}, raising=False)
    monkeypatch.setattr(api, "_RATE_LIMIT_BASE_COOLDOWN", 10.0, raising=False)
    monkeypatch.setattr(api, "_RATE_LIMIT_MAX_COOLDOWN", 120.0, raising=False)
    monkeypatch.setattr(api, "_RATE_LIMIT_BACKOFF_DECAY", 300.0, raising=False)

    before = time.monotonic()
    penalty = await api.mark_api_key_rate_limited(key)

    assert penalty is not None
    assert penalty.cooldown_seconds == pytest.approx(10.0)
    assert penalty.strike_count == 1
    assert key not in api._working_keys
    assert key in api._quarantine
    assert api._quarantine[key] > before
    assert key in api._rate_limit_state

    second_penalty = await api.mark_api_key_rate_limited(key)

    assert second_penalty is not None
    assert second_penalty.cooldown_seconds == pytest.approx(20.0)
    assert second_penalty.strike_count == 2
    assert api._rate_limit_state[key].strike_count == 2


@pytest.mark.anyio
async def test_rate_limit_strikes_reset_after_success(monkeypatch):
    key = "encrypted-key"
    monkeypatch.setattr(api, "_working_keys", [], raising=False)
    monkeypatch.setattr(api, "_non_working_keys", [], raising=False)
    monkeypatch.setattr(api, "_quarantine", {}, raising=False)
    monkeypatch.setattr(api, "_rate_limit_state", {}, raising=False)
    monkeypatch.setattr(api, "_RATE_LIMIT_BASE_COOLDOWN", 5.0, raising=False)
    monkeypatch.setattr(api, "_RATE_LIMIT_MAX_COOLDOWN", 120.0, raising=False)
    monkeypatch.setattr(api, "_RATE_LIMIT_BACKOFF_DECAY", 300.0, raising=False)

    await api.mark_api_key_rate_limited(key)

    async def fake_execute_query(_query, _params=None, *, fetch_one=False, fetch_all=False):
        return None, 1

    monkeypatch.setattr(api.mysql, "execute_query", fake_execute_query)
    await api.set_api_key_working(key)

    penalty = await api.mark_api_key_rate_limited(key)

    assert penalty is not None
    assert penalty.cooldown_seconds == pytest.approx(5.0)
    assert penalty.strike_count == 1
