from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import os
import sys
from pathlib import Path
from typing import cast

import pytest
from discord.ext import commands

os.environ.setdefault("FERNET_SECRET_KEY", "DeJ3sXDDTTbikeRSJzRgg8r_Ch61_NbE8D3LWnLOJO4=")
sys.path.append(str(Path(__file__).resolve().parents[1]))

from modules.captcha.config import CaptchaStreamConfig
from modules.captcha.client import CaptchaApiClient
from modules.captcha.models import CaptchaCallbackPayload, CaptchaPayloadError
from modules.captcha.sessions import CaptchaSession, CaptchaSessionStore
from modules.captcha.processor import (
    FailureAction,
    _extract_action_strings,
    _normalize_failure_actions,
)
from modules.captcha.stream import CaptchaStreamListener, RedisConnectionError

def test_session_store_round_trip() -> None:
    async def run() -> None:
        store = CaptchaSessionStore()
        expires = datetime.now(timezone.utc) + timedelta(minutes=5)
        session = CaptchaSession(guild_id=1, user_id=2, token="abc", expires_at=expires)

        await store.put(session)
        fetched = await store.get(1, 2)
        assert fetched is session

        await store.remove(1, 2)
        assert await store.get(1, 2) is None

    asyncio.run(run())

def test_session_store_expires_automatically() -> None:
    async def run() -> None:
        store = CaptchaSessionStore()
        expired = datetime.now(timezone.utc) - timedelta(seconds=1)
        session = CaptchaSession(guild_id=5, user_id=6, token="xyz", expires_at=expired)

        await store.put(session)
        assert await store.get(5, 6) is None

    asyncio.run(run())

def test_callback_payload_parses_new_format() -> None:
    payload = CaptchaCallbackPayload.from_mapping(
        {
            "guildId": "123",
            "userId": "456",
            "token": "token-123",
            "status": "passed",
            "state": "opaque",
        }
    )

    assert payload.guild_id == 123
    assert payload.user_id == 456
    assert payload.token == "token-123"
    assert payload.success is True
    assert payload.status == "passed"
    assert payload.state == "opaque"

def test_callback_payload_handles_boolean_success() -> None:
    payload = CaptchaCallbackPayload.from_mapping(
        {
            "guild_id": "42",
            "user_id": "24",
            "success": False,
            "token": "abc",
        }
    )

    assert payload.success is False
    assert payload.status == "failed"
    assert payload.failure_reason is None

def test_callback_payload_missing_token() -> None:
    with pytest.raises(CaptchaPayloadError):
        CaptchaCallbackPayload.from_mapping(
            {
                "guild_id": "1",
                "user_id": "2",
                "status": "passed",
            }
        )


def test_start_session_includes_callback_url(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        client = CaptchaApiClient("https://api.example.com/captcha", "token-123")

        class DummyResponse:
            def __init__(self) -> None:
                self.status = 200

            async def json(self) -> dict[str, str]:
                return {
                    "token": "abc",
                    "guildId": "1",
                    "userId": "2",
                    "verificationUrl": "https://verify",
                    "expiresAt": 1,
                }

            async def __aenter__(self) -> "DummyResponse":
                return self

            async def __aexit__(self, *args: object) -> None:
                return None

        class DummySession:
            def __init__(self) -> None:
                self.payload: dict[str, object] | None = None

            def post(self, url: str, *, json: dict[str, object], headers: dict[str, str]) -> DummyResponse:
                self.payload = json
                return DummyResponse()

        session = DummySession()

        async def fake_ensure_session() -> DummySession:
            return session

        monkeypatch.setattr(client, "_ensure_session", fake_ensure_session)

        await client.start_session(1, 2, callback_url="https://bot.example.com/captcha/callback")

        assert session.payload is not None
        assert session.payload["callbackUrl"] == "https://bot.example.com/captcha/callback"

    asyncio.run(run())


def test_stream_config_disabled_without_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAPTCHA_REDIS_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("CAPTCHA_STREAM_ENABLED", raising=False)

    config = CaptchaStreamConfig.from_env()

    assert config.redis_url is None
    assert config.enabled is False


def test_stream_config_uses_explicit_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAPTCHA_REDIS_URL", "  redis://localhost:6379/2  ")
    monkeypatch.delenv("CAPTCHA_STREAM_ENABLED", raising=False)

    config = CaptchaStreamConfig.from_env()

    assert config.redis_url == "redis://localhost:6379/2"
    assert config.enabled is True


def test_normalize_failure_actions_handles_mixed_entries() -> None:
    actions = _normalize_failure_actions(
        [
            "kick",
            "timeout:30m",
            {"value": "log", "extra": "123"},
        ]
    )

    assert [action.action for action in actions] == [
        "kick",
        "timeout",
        "log",
    ]
    assert actions[1].extra == "30m"
    assert actions[2].extra == "123"


def test_normalize_failure_actions_supports_nested_extra() -> None:
    actions = _normalize_failure_actions(
        [
            {
                "value": "timeout",
                "extra": {"value": "15m"},
            }
        ]
    )

    assert actions == [FailureAction(action="timeout", extra="15m")]


def test_normalize_failure_actions_filters_invalid_entries() -> None:
    actions = _normalize_failure_actions([{}, 42, "   "])

    assert actions == []


def test_extract_action_strings_filters_entries() -> None:
    actions = _extract_action_strings([
        " give_role:1  ",
        "",
        None,
        "strike",
        42,
    ])

    assert actions == ["give_role:1", "strike"]


def test_extract_action_strings_handles_single_string() -> None:
    assert _extract_action_strings("take_role:5") == ["take_role:5"]


def test_stream_listener_handles_redis_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class DummyPool:
        def __init__(self) -> None:
            self.disconnected = False

        async def disconnect(self) -> None:
            self.disconnected = True

    class DummyRedis:
        def __init__(self) -> None:
            self.closed = False
            self.connection_pool = DummyPool()

        async def xgroup_create(self, *args: object, **kwargs: object) -> None:
            raise RedisConnectionError("boom")

        async def close(self) -> None:
            self.closed = True

    dummy_redis = DummyRedis()

    def fake_from_url(url: str, *, decode_responses: bool) -> DummyRedis:
        assert decode_responses is True
        assert url == "redis://localhost:6379/0"
        return dummy_redis

    monkeypatch.setattr("modules.captcha.stream.redis_from_url", fake_from_url)

    config = CaptchaStreamConfig(
        enabled=True,
        redis_url="redis://localhost:6379/0",
        stream="captcha:callbacks",
        group="modbot",
        consumer_name="consumer",
        block_ms=1000,
        batch_size=5,
        max_requeue_attempts=3,
        shared_secret=None,
    )

    listener = CaptchaStreamListener(cast(commands.Bot, object()), config, CaptchaSessionStore())

    async def run() -> None:
        started = await listener.start()
        assert started is False
        assert listener._redis is None  # type: ignore[attr-defined]

    asyncio.run(run())
