from __future__ import annotations

import asyncio
import logging
import types

import pytest

from modules.post_stats import topgg_poster


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _BotStub:
    def __init__(self) -> None:
        self.guilds = [object()]
        self.user = types.SimpleNamespace(id=123456789)


class _DummySession:
    def __init__(self, *, response_factory):
        self._response_factory = response_factory

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def post(self, *_, **__):
        return self._response_factory()


async def test_post_guild_count_skips_without_token(monkeypatch):
    monkeypatch.delenv("TOPGG_API_TOKEN", raising=False)
    bot = _BotStub()
    called = {"count": 0}

    def _session_factory(**_):
        called["count"] += 1
        return _DummySession(response_factory=lambda: None)

    await topgg_poster._post_guild_count_once(bot, session_factory=_session_factory)
    assert called["count"] == 0


async def test_post_guild_count_handles_timeout(monkeypatch, caplog):
    monkeypatch.setenv("TOPGG_API_TOKEN", "token")
    bot = _BotStub()

    class _TimeoutResponse:
        async def __aenter__(self):
            raise asyncio.TimeoutError

        async def __aexit__(self, *_):
            return False

    calls = {"count": 0}

    def _session_factory(**_):
        calls["count"] += 1
        return _DummySession(response_factory=_TimeoutResponse)

    with caplog.at_level(logging.WARNING):
        await topgg_poster._post_guild_count_once(bot, session_factory=_session_factory)
    assert calls["count"] == 1
    assert "timed out" in caplog.text.lower()


async def test_post_guild_count_logs_non_200(monkeypatch, capsys):
    monkeypatch.setenv("TOPGG_API_TOKEN", "token")
    bot = _BotStub()

    class _FailureResponse:
        status = 500

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def text(self):
            return "oops"

    def _session_factory(**_):
        return _DummySession(response_factory=_FailureResponse)

    await topgg_poster._post_guild_count_once(bot, session_factory=_session_factory)
    captured = capsys.readouterr()
    assert "Failed to post server count" in captured.out
