import os
import sys
import time
import types
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("FERNET_SECRET_KEY", Fernet.generate_key().decode())

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

mysql_stub = types.ModuleType("modules.utils.mysql")


async def _unpatched_execute_query(*_args, **_kwargs):
    raise AssertionError("execute_query should be monkeypatched in tests")


mysql_stub.execute_query = _unpatched_execute_query
sys.modules.setdefault("modules.utils.mysql", mysql_stub)

discord_utils_stub = types.ModuleType("modules.utils.discord_utils")


async def _safe_get_user_stub(*_args, **_kwargs):
    return None


discord_utils_stub.safe_get_user = _safe_get_user_stub
sys.modules.setdefault("modules.utils.discord_utils", discord_utils_stub)

from modules.utils import api


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_set_api_key_not_working_moves_key_out_of_working_pool(monkeypatch):
    key = "encrypted-key"

    monkeypatch.setattr(api, "_working_keys", [key])
    monkeypatch.setattr(api, "_non_working_keys", [], raising=False)
    monkeypatch.setattr(api, "_quarantine", {}, raising=False)

    async def fake_execute_query(_query, _params=None, *, fetch_one=False, fetch_all=False):
        return None, 1

    monkeypatch.setattr(api.mysql, "execute_query", fake_execute_query)

    await api.set_api_key_not_working(key, bot=None)

    assert key not in api._working_keys
    assert key in api._non_working_keys
    assert key in api._quarantine
    assert api._quarantine[key] > time.monotonic()
