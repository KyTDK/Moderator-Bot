from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import os
import socket
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from discord.ext import commands

os.environ.setdefault("FERNET_SECRET_KEY", "DeJ3sXDDTTbikeRSJzRgg8r_Ch61_NbE8D3LWnLOJO4=")
sys.path.append(str(Path(__file__).resolve().parents[1]))

from modules.captcha.client import CaptchaApiClient
from modules.captcha.models import CaptchaCallbackPayload, CaptchaPayloadError
from modules.captcha.sessions import CaptchaSession, CaptchaSessionStore
from modules.captcha.config import CaptchaWebhookConfig
from modules.captcha.webhook import CaptchaWebhookServer
from modules.captcha.processor import FailureAction, _normalize_failure_actions

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


def test_webhook_config_uses_public_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAPTCHA_WEBHOOK_HOST", raising=False)
    monkeypatch.delenv("CAPTCHA_WEBHOOK_PORT", raising=False)
    monkeypatch.delenv("CAPTCHA_WEBHOOK_TOKEN", raising=False)
    monkeypatch.delenv("CAPTCHA_API_TOKEN", raising=False)
    monkeypatch.delenv("CAPTCHA_SHARED_SECRET", raising=False)
    monkeypatch.setenv("CAPTCHA_WEBHOOK_ENABLED", "true")
    monkeypatch.setenv("CAPTCHA_WEBHOOK_PUBLIC_URL", "https://example.com/bot")

    config = CaptchaWebhookConfig.from_env()

    assert config.enabled is True
    assert config.callback_url == "https://example.com/bot/captcha/callback"


def test_webhook_config_loopback_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAPTCHA_WEBHOOK_HOST", "localhost")
    monkeypatch.setenv("CAPTCHA_WEBHOOK_PORT", "9000")
    monkeypatch.setenv("CAPTCHA_WEBHOOK_ENABLED", "true")
    monkeypatch.delenv("CAPTCHA_WEBHOOK_PUBLIC_URL", raising=False)
    monkeypatch.delenv("CAPTCHA_WEBHOOK_TOKEN", raising=False)
    monkeypatch.delenv("CAPTCHA_API_TOKEN", raising=False)
    monkeypatch.delenv("CAPTCHA_SHARED_SECRET", raising=False)

    config = CaptchaWebhookConfig.from_env()

    assert config.callback_url == "http://localhost:9000/captcha/callback"


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


def _build_config(*, enabled: bool, host: str = "127.0.0.1", port: int = 8080) -> CaptchaWebhookConfig:
    return CaptchaWebhookConfig(
        enabled=enabled,
        host=host,
        port=port,
        token=None,
        shared_secret=None,
        public_url=None,
    )


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_webhook_start_returns_false_when_disabled() -> None:
    async def run() -> None:
        bot = MagicMock(spec=commands.Bot)
        store = CaptchaSessionStore()
        webhook = CaptchaWebhookServer(bot, _build_config(enabled=False), store)

        started = await webhook.start()

        assert started is False
        assert webhook.started is False

    asyncio.run(run())


def test_webhook_start_returns_true_when_enabled() -> None:
    async def run() -> None:
        bot = MagicMock(spec=commands.Bot)
        store = CaptchaSessionStore()
        port = _get_free_port()
        webhook = CaptchaWebhookServer(
            bot,
            _build_config(enabled=True, port=port),
            store,
        )

        started = await webhook.start()

        assert started is True
        assert webhook.started is True

        await webhook.stop()
        assert webhook.started is False

    asyncio.run(run())


def test_normalize_failure_actions_handles_mixed_entries() -> None:
    actions = _normalize_failure_actions(
        [
            "kick",
            "timeout:30m",
            {"value": "log", "extra": "123"},
            {"value": "dm_staff", "extra": "1, 2"},
        ]
    )

    assert [action.action for action in actions] == [
        "kick",
        "timeout",
        "log",
        "dm_staff",
    ]
    assert actions[1].extra == "30m"
    assert actions[2].extra == "123"
    assert actions[3].extra == "1, 2"


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
