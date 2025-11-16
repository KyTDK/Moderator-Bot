from __future__ import annotations

import base64
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

os.environ.setdefault("FERNET_SECRET_KEY", base64.urlsafe_b64encode(b"0" * 32).decode())

from conftest import _restore_real_modules

_restore_real_modules()

import bot
from modules.core.config import RuntimeConfig, ShardConfig
from modules.utils.mysql import ShardAssignment, ShardClaimError


def _install_config(monkeypatch: pytest.MonkeyPatch, **shard_overrides) -> RuntimeConfig:
    shard_defaults = {
        "total_shards": 2,
        "preferred_shard": 0,
        "stale_seconds": 300,
        "heartbeat_seconds": 30,
        "instance_heartbeat_seconds": 5,
        "instance_id": "test-instance",
        "standby_when_full": True,
        "standby_poll_seconds": 0,
    }
    shard_defaults.update(shard_overrides)

    config = RuntimeConfig(
        token="test-token",
        log_level="INFO",
        log_cog_loads=False,
        shard=ShardConfig(**shard_defaults),
    )

    monkeypatch.setattr(bot, "load_runtime_config", lambda: config)
    monkeypatch.setattr(bot, "configure_logging", lambda *_args, **_kwargs: None)
    return config


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def fake_bot(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    holder: dict[str, object] = {}

    class FakeBot:
        def __init__(self, **_kwargs):
            self.assignments = []
            self.prepare_standby_calls = []
            self.start_calls = []
            self.connect_calls = []
            self.push_status_calls = []
            self.close_calls = 0
            self._closed = False
            holder["instance"] = self

        async def prepare_standby(self, token: str) -> None:
            self.prepare_standby_calls.append(token)

        def set_shard_assignment(self, assignment: ShardAssignment) -> None:
            self.assignments.append(assignment)

        async def push_status(self, status: str, *, last_error: str | None = None) -> None:
            self.push_status_calls.append((status, last_error))

        async def start(self, token: str) -> None:
            self.start_calls.append(token)

        async def connect(self, reconnect: bool = True) -> None:
            self.connect_calls.append(reconnect)

        async def close(self) -> None:
            self.close_calls += 1
            self._closed = True

        def is_closed(self) -> bool:
            return self._closed

    monkeypatch.setattr(bot, "ModeratorBot", FakeBot)
    return holder


@pytest.fixture
def mysql_stubs(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    stubs = {
        "initialise_and_get_pool": AsyncMock(),
        "recover_stuck_shards": AsyncMock(return_value=0),
        "update_instance_heartbeat": AsyncMock(),
        "claim_shard": AsyncMock(),
        "update_shard_status": AsyncMock(),
        "clear_instance_heartbeat": AsyncMock(),
        "release_shard": AsyncMock(return_value=True),
        "close_pool": AsyncMock(),
    }

    for name, stub in stubs.items():
        monkeypatch.setattr(bot.mysql, name, stub)

    return SimpleNamespace(**stubs)


@pytest.mark.anyio
async def test_main_claims_shard_and_starts_without_standby(monkeypatch, fake_bot, mysql_stubs):
    config = _install_config(monkeypatch, preferred_shard=1)
    assignment = ShardAssignment(shard_id=1, shard_count=config.shard.total_shards)
    mysql_stubs.claim_shard.return_value = assignment

    await bot._main()

    bot_instance = fake_bot["instance"]
    assert bot_instance.assignments[-1] == assignment
    assert bot_instance.prepare_standby_calls == []
    assert bot_instance.start_calls == [config.token]
    assert bot_instance.connect_calls == []
    assert bot_instance.push_status_calls == [("starting", None)]
    assert mysql_stubs.initialise_and_get_pool.await_count == 1
    assert mysql_stubs.update_instance_heartbeat.await_count == 1
    assert mysql_stubs.recover_stuck_shards.await_count == 1
    assert mysql_stubs.claim_shard.await_count == 1
    assert mysql_stubs.release_shard.await_count == 1
    assert mysql_stubs.release_shard.await_args.args == (
        assignment.shard_id,
        config.shard.instance_id,
    )
    assert mysql_stubs.clear_instance_heartbeat.await_count == 1
    assert mysql_stubs.close_pool.await_count == 1


@pytest.mark.anyio
async def test_main_enters_standby_when_shard_claim_initially_fails(monkeypatch, fake_bot, mysql_stubs):
    config = _install_config(monkeypatch, preferred_shard=0, standby_poll_seconds=0)
    assignment = ShardAssignment(shard_id=0, shard_count=config.shard.total_shards)
    mysql_stubs.claim_shard.side_effect = [
        ShardClaimError("pool full"),
        assignment,
    ]

    sleep_stub = AsyncMock()
    monkeypatch.setattr(bot.asyncio, "sleep", sleep_stub)

    await bot._main()

    bot_instance = fake_bot["instance"]
    assert bot_instance.prepare_standby_calls == [config.token]
    assert bot_instance.start_calls == []
    assert bot_instance.connect_calls == [True]
    assert mysql_stubs.recover_stuck_shards.await_count == 2
    assert mysql_stubs.update_instance_heartbeat.await_count == 2
    assert mysql_stubs.claim_shard.await_count == 2
    assert sleep_stub.await_count == 1


@pytest.mark.anyio
async def test_main_uses_fast_failover_windows(monkeypatch, fake_bot, mysql_stubs):
    config = _install_config(
        monkeypatch,
        preferred_shard=2,
        total_shards=4,
        heartbeat_seconds=18,
        stale_seconds=900,
        instance_heartbeat_seconds=9,
    )
    assignment = ShardAssignment(shard_id=2, shard_count=config.shard.total_shards)
    mysql_stubs.claim_shard.return_value = assignment

    await bot._main()

    expected_failover = max(config.shard.heartbeat_seconds * 2, 30)
    expected_stale = max(30, min(config.shard.stale_seconds, expected_failover))
    expected_instance_failover = max(
        config.shard.instance_heartbeat_seconds * 2,
        config.shard.instance_heartbeat_seconds + 3,
        6,
    )

    first_recover_call = mysql_stubs.recover_stuck_shards.await_args_list[0]
    assert first_recover_call.args[0] == expected_stale
    assert first_recover_call.kwargs["instance_stale_after_seconds"] == expected_instance_failover

    claim_kwargs = mysql_stubs.claim_shard.await_args.kwargs
    assert claim_kwargs["stale_after_seconds"] == expected_stale
    assert claim_kwargs["instance_stale_after_seconds"] == expected_instance_failover
    assert claim_kwargs["total_shards"] == config.shard.total_shards
    assert claim_kwargs["preferred_shard"] == config.shard.preferred_shard
