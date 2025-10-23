from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from discord.ext import commands

os.environ.setdefault("FERNET_SECRET_KEY", "DeJ3sXDDTTbikeRSJzRgg8r_Ch61_NbE8D3LWnLOJO4=")
sys.path.append(str(Path(__file__).resolve().parents[1]))

from modules.captcha.config import CaptchaStreamConfig
from modules.captcha.client import CaptchaApiClient
from modules.captcha.models import (
    CaptchaCallbackPayload,
    CaptchaPayloadError,
    CaptchaProcessingError,
    CaptchaSettingsUpdatePayload,
)
from modules.captcha.sessions import CaptchaSession, CaptchaSessionStore
from modules.captcha.processor import (
    CaptchaCallbackProcessor,
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

def test_session_store_supports_indefinite_sessions() -> None:
    async def run() -> None:
        store = CaptchaSessionStore()
        session = CaptchaSession(guild_id=3, user_id=4, token="indef", expires_at=None)

        await store.put(session)
        assert await store.get(3, 4) is session

    asyncio.run(run())

def test_session_store_peek_does_not_evict() -> None:
    async def run() -> None:
        store = CaptchaSessionStore()
        expires = datetime.now(timezone.utc) + timedelta(minutes=1)
        session = CaptchaSession(guild_id=9, user_id=10, token="peek", expires_at=expires)

        await store.put(session)
        peeked = await store.peek(9, 10)
        assert peeked is session
        assert await store.get(9, 10) is session

    asyncio.run(run())

def test_processor_embed_expiry_disabled() -> None:
    store = CaptchaSessionStore()
    processor = CaptchaCallbackProcessor(cast(commands.Bot, object()), store)

    expires = processor._determine_embed_expiry({"captcha-grace-period": "0m"})

    assert expires is None

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


def test_settings_update_payload_parses() -> None:
    payload = CaptchaSettingsUpdatePayload.from_mapping(
        {
            "eventType": "captcha.settings.updated",
            "guildId": "123",
            "key": "captcha-delivery-method",
            "value": "embed",
            "updatedAt": "1700000000000",
            "eventVersion": 1,
        }
    )

    assert payload.guild_id == 123
    assert payload.key == "captcha-delivery-method"
    assert payload.value == "embed"
    assert payload.updated_at == 1700000000000
    assert payload.version == 1


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


def test_fetch_guild_config_parses_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        client = CaptchaApiClient("https://api.example.com/captcha", "token-xyz")

        class DummyResponse:
            def __init__(self) -> None:
                self.status = 200

            async def json(self) -> dict[str, object]:
                return {
                    "guildId": "123",
                    "delivery": {
                        "method": "embed",
                        "requiresLogin": True,
                        "embedChannelId": "789",
                    },
                    "captcha": {
                        "provider": "turnstile",
                        "providerLabel": "Cloudflare Turnstile",
                    },
                }

            async def __aenter__(self) -> "DummyResponse":
                return self

            async def __aexit__(self, *args: object) -> None:
                return None

        class DummySession:
            def __init__(self) -> None:
                self.params: dict[str, object] | None = None
                self.headers: dict[str, str] | None = None

            def get(self, url: str, *, params: dict[str, object], headers: dict[str, str]) -> DummyResponse:
                assert url == "https://api.example.com/captcha"
                self.params = params
                self.headers = headers
                return DummyResponse()

        dummy_session = DummySession()

        async def fake_ensure_session() -> DummySession:
            return dummy_session

        monkeypatch.setattr(client, "_ensure_session", fake_ensure_session)

        config = await client.fetch_guild_config(123)

        assert dummy_session.params == {"gid": "123"}
        assert dummy_session.headers == {"Authorization": "Bot token-xyz"}
        assert config.guild_id == 123
        assert config.delivery.method == "embed"
        assert config.delivery.requires_login is True
        assert config.delivery.embed_channel_id == 789
        assert config.provider == "turnstile"
        assert config.provider_label == "Cloudflare Turnstile"

    asyncio.run(run())


def test_stream_config_disabled_without_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAPTCHA_REDIS_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("CAPTCHA_STREAM_ENABLED", raising=False)

    config = CaptchaStreamConfig.from_env()

    assert config.redis_url is None
    assert config.enabled is False
    assert config.start_id == "$"


def test_stream_config_uses_explicit_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAPTCHA_REDIS_URL", "  redis://localhost:6379/2  ")
    monkeypatch.delenv("CAPTCHA_STREAM_ENABLED", raising=False)

    config = CaptchaStreamConfig.from_env()

    assert config.redis_url == "redis://localhost:6379/2"
    assert config.enabled is True
    assert config.start_id == "$"


def test_stream_config_honours_start_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAPTCHA_REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("CAPTCHA_STREAM_START_ID", "0")
    monkeypatch.delenv("CAPTCHA_STREAM_ENABLED", raising=False)

    config = CaptchaStreamConfig.from_env()

    assert config.start_id == "0"


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
        start_id="$",
        shared_secret=None,
        pending_auto_claim_ms=0,
    )

    listener = CaptchaStreamListener(cast(commands.Bot, object()), config, CaptchaSessionStore())

    async def run() -> None:
        started = await listener.start()
        assert started is False
        assert listener._redis is None  # type: ignore[attr-defined]

    asyncio.run(run())


def test_stream_listener_invokes_settings_callback() -> None:
    config = CaptchaStreamConfig(
        enabled=True,
        redis_url="redis://localhost:6379/0",
        stream="captcha:callbacks",
        group="modbot",
        consumer_name="consumer",
        block_ms=1000,
        batch_size=5,
        max_requeue_attempts=3,
        start_id="$",
        shared_secret=None,
        pending_auto_claim_ms=0,
    )

    received: list[CaptchaSettingsUpdatePayload] = []

    async def callback(payload: CaptchaSettingsUpdatePayload) -> None:
        received.append(payload)

    listener = CaptchaStreamListener(
        cast(commands.Bot, object()),
        config,
        CaptchaSessionStore(),
        callback,
    )

    async def run() -> None:
        listener._redis = cast(Any, object())  # type: ignore[attr-defined]
        fields = {
            "payload": json.dumps(
                {
                    "eventType": "captcha.settings.updated",
                    "guildId": "123",
                    "key": "captcha-delivery-method",
                    "value": "embed",
                    "updatedAt": 1700000000000,
                    "eventVersion": 1,
                }
            )
        }
        result = await listener._process_message("captcha:callbacks", "1-0", fields)
        assert result is True

    asyncio.run(run())

    assert len(received) == 1
    payload = received[0]
    assert payload.guild_id == 123
    assert payload.key == "captcha-delivery-method"
    assert payload.value == "embed"




def test_stream_listener_claims_stale_messages() -> None:
    processed: list[CaptchaCallbackPayload] = []
    acked: list[tuple[str, str, str]] = []

    class DummyBot:
        def get_guild(self, guild_id: int) -> object | None:
            assert guild_id == 123
            return object()

    class DummyRedis:
        def __init__(self) -> None:
            self.calls = 0

        async def xautoclaim(
            self,
            stream: str,
            group: str,
            consumer: str,
            min_idle_time: int,
            start: str,
            *,
            count: int | None = None,
        ) -> tuple[str, list[tuple[str, dict[str, Any]]]]:
            self.calls += 1
            assert stream == "captcha:callbacks"
            assert group == "modbot"
            assert consumer == "consumer"
            assert min_idle_time == 1000
            if self.calls == 1:
                payload = {
                    "eventType": "captcha.verification.completed",
                    "guildId": "123",
                    "userId": "456",
                    "token": "abc",
                    "success": True,
                    "status": "passed",
                    "metadata": {},
                }
                return "0-0", [("1-0", {"payload": json.dumps(payload)})]
            return "0-0", []

        async def xack(self, stream: str, group: str, message_id: str) -> None:
            acked.append((stream, group, message_id))

    class DummyProcessor:
        async def process(self, payload: CaptchaCallbackPayload, **_: Any) -> None:
            processed.append(payload)

    config = CaptchaStreamConfig(
        enabled=True,
        redis_url="redis://localhost:6379/0",
        stream="captcha:callbacks",
        group="modbot",
        consumer_name="consumer",
        block_ms=1000,
        batch_size=5,
        max_requeue_attempts=3,
        start_id="$",
        shared_secret=None,
        pending_auto_claim_ms=1000,
    )

    listener = CaptchaStreamListener(
        cast(commands.Bot, DummyBot()),
        config,
        CaptchaSessionStore(),
    )
    listener._redis = cast(Any, DummyRedis())  # type: ignore[attr-defined]
    listener._processor = cast(Any, DummyProcessor())  # type: ignore[attr-defined]

    async def run() -> None:
        await listener._claim_stale_messages()

    asyncio.run(run())

    assert len(processed) == 1
    payload = processed[0]
    assert payload.guild_id == 123
    assert payload.user_id == 456
    assert listener.last_message_id == "1-0"
    assert acked == [(config.stream, config.group, "1-0")]


def test_captcha_processor_recovers_embed_session(monkeypatch: pytest.MonkeyPatch) -> None:
    class DummyMember:
        def __init__(self, user_id: int, guild: "DummyGuild") -> None:
            self.id = user_id
            self.guild = guild
            self.roles: list[int] = []
            self.mention = f"<@{user_id}>"

    class DummyGuild:
        def __init__(self, guild_id: int) -> None:
            self.id = guild_id
            self.name = "Guild"
            self._member: DummyMember | None = None

        def set_member(self, member: DummyMember) -> None:
            self._member = member

        def get_member(self, user_id: int) -> DummyMember | None:
            if self._member and self._member.id == user_id:
                return self._member
            return None

        async def fetch_member(self, user_id: int) -> DummyMember:
            member = self.get_member(user_id)
            if member is None:
                raise AssertionError("fetch_member should not be called")
            return member

    class DummyBot:
        def __init__(self, guild: DummyGuild) -> None:
            self._guild = guild

        def get_guild(self, guild_id: int) -> DummyGuild | None:
            if self._guild.id == guild_id:
                return self._guild
            return None

        async def fetch_guild(self, guild_id: int) -> DummyGuild:
            return self._guild

    store = CaptchaSessionStore()
    guild_id = 123
    user_id = 456
    dummy_guild = DummyGuild(guild_id)
    dummy_member = DummyMember(user_id, dummy_guild)
    dummy_guild.set_member(dummy_member)
    bot = DummyBot(dummy_guild)

    monkeypatch.setattr("modules.captcha.processor.discord.Member", DummyMember)

    processor = CaptchaCallbackProcessor(cast(commands.Bot, bot), store)

    async def fake_get_settings(guild: int, keys: list[str]) -> dict[str, Any]:
        return {
            "captcha-verification-enabled": True,
            "captcha-success-actions": None,
            "captcha-failure-actions": None,
            "captcha-max-attempts": None,
            "captcha-log-channel": None,
            "captcha-delivery-method": "embed",
            "captcha-grace-period": "10m",
        }

    async def fake_log_to_channel(*args: Any, **kwargs: Any) -> None:
        return None

    async def fake_perform_action(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr("modules.utils.mysql.get_settings", fake_get_settings)
    monkeypatch.setattr("modules.utils.mod_logging.log_to_channel", fake_log_to_channel)
    monkeypatch.setattr("modules.moderation.strike.perform_disciplinary_action", fake_perform_action)

    payload = CaptchaCallbackPayload.from_mapping(
        {
            "guildId": str(guild_id),
            "userId": str(user_id),
            "token": "embed-token",
            "status": "passed",
        }
    )

    async def run() -> None:
        result = await processor.process(payload)
        assert result.status == "ok"
        assert result.roles_applied == 0
        assert await store.get(guild_id, user_id) is None

    asyncio.run(run())


def test_captcha_processor_requires_session_for_dm(monkeypatch: pytest.MonkeyPatch) -> None:
    class DummyMember:
        def __init__(self, user_id: int, guild: "DummyGuild") -> None:
            self.id = user_id
            self.guild = guild
            self.mention = f"<@{user_id}>"

    class DummyGuild:
        def __init__(self, guild_id: int) -> None:
            self.id = guild_id
            self._member: DummyMember | None = None

        def set_member(self, member: DummyMember) -> None:
            self._member = member

        def get_member(self, user_id: int) -> DummyMember | None:
            if self._member and self._member.id == user_id:
                return self._member
            return None

        async def fetch_member(self, user_id: int) -> DummyMember:
            raise AssertionError("fetch_member should not be called")

    class DummyBot:
        def __init__(self, guild: DummyGuild) -> None:
            self._guild = guild

        def get_guild(self, guild_id: int) -> DummyGuild | None:
            if self._guild.id == guild_id:
                return self._guild
            return None

        async def fetch_guild(self, guild_id: int) -> DummyGuild:
            return self._guild

    store = CaptchaSessionStore()
    guild_id = 987
    user_id = 654
    dummy_guild = DummyGuild(guild_id)
    dummy_member = DummyMember(user_id, dummy_guild)
    dummy_guild.set_member(dummy_member)
    bot = DummyBot(dummy_guild)

    monkeypatch.setattr("modules.captcha.processor.discord.Member", DummyMember)

    processor = CaptchaCallbackProcessor(cast(commands.Bot, bot), store)

    async def fake_get_settings(guild: int, keys: list[str]) -> dict[str, Any]:
        return {
            "captcha-verification-enabled": True,
            "captcha-success-actions": None,
            "captcha-failure-actions": None,
            "captcha-max-attempts": None,
            "captcha-log-channel": None,
            "captcha-delivery-method": "dm",
            "captcha-grace-period": None,
        }

    monkeypatch.setattr("modules.utils.mysql.get_settings", fake_get_settings)

    payload = CaptchaCallbackPayload.from_mapping(
        {
            "guildId": str(guild_id),
            "userId": str(user_id),
            "token": "dm-token",
            "status": "passed",
        }
    )

    async def run() -> None:
        with pytest.raises(CaptchaProcessingError) as excinfo:
            await processor.process(payload)
        assert excinfo.value.code == "unknown_token"

    asyncio.run(run())

def test_captcha_timeout_ignored_when_grace_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DummyMember:
        def __init__(self, user_id: int, guild: "DummyGuild") -> None:
            self.id = user_id
            self.guild = guild
            self.mention = f"<@{user_id}>"

    class DummyGuild:
        def __init__(self, guild_id: int) -> None:
            self.id = guild_id
            self._member: DummyMember | None = None

        def set_member(self, member: DummyMember) -> None:
            self._member = member

        def get_member(self, user_id: int) -> DummyMember | None:
            if self._member and self._member.id == user_id:
                return self._member
            return None

        async def fetch_member(self, user_id: int) -> DummyMember:
            member = self.get_member(user_id)
            if member is None:
                raise AssertionError("fetch_member should not be called")
            return member

    class DummyBot:
        def __init__(self, guild: DummyGuild) -> None:
            self._guild = guild

        def get_guild(self, guild_id: int) -> DummyGuild | None:
            if self._guild.id == guild_id:
                return self._guild
            return None

        async def fetch_guild(self, guild_id: int) -> DummyGuild:
            return self._guild

    store = CaptchaSessionStore()
    guild_id = 2468
    user_id = 1357
    dummy_guild = DummyGuild(guild_id)
    dummy_member = DummyMember(user_id, dummy_guild)
    dummy_guild.set_member(dummy_member)
    bot = DummyBot(dummy_guild)

    monkeypatch.setattr("modules.captcha.processor.discord.Member", DummyMember)

    processor = CaptchaCallbackProcessor(cast(commands.Bot, bot), store)

    async def fake_get_settings(guild: int, keys: list[str]) -> dict[str, Any]:
        return {
            "captcha-verification-enabled": True,
            "captcha-success-actions": None,
            "captcha-failure-actions": ["kick"],
            "captcha-max-attempts": None,
            "captcha-log-channel": None,
            "captcha-delivery-method": "dm",
            "captcha-grace-period": "0m",
        }

    actions_called = False

    async def fake_perform_action(*args: Any, **kwargs: Any) -> None:
        nonlocal actions_called
        actions_called = True
        return None

    async def fake_log_to_channel(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr("modules.utils.mysql.get_settings", fake_get_settings)
    monkeypatch.setattr(
        "modules.moderation.strike.perform_disciplinary_action", fake_perform_action
    )
    monkeypatch.setattr("modules.utils.mod_logging.log_to_channel", fake_log_to_channel)

    payload = CaptchaCallbackPayload.from_mapping(
        {
            "guildId": str(guild_id),
            "userId": str(user_id),
            "token": "dm-token",
            "status": "expired",
            "success": False,
            "failure_reason": "Captcha verification timed out.",
            "metadata": {"timeout": True, "reason": "expired"},
        }
    )

    session = CaptchaSession(
        guild_id=guild_id,
        user_id=user_id,
        token="dm-token",
        expires_at=None,
        delivery_method="dm",
    )

    async def run() -> None:
        await store.put(session)
        result = await processor.process(payload)
        assert result.status == "timeout_ignored"
        assert result.roles_applied == 0
        assert not actions_called
        assert await store.get(guild_id, user_id) is None

    asyncio.run(run())
def test_captcha_failure_keeps_session_when_attempts_remaining(monkeypatch: pytest.MonkeyPatch) -> None:
    class DummyMember:
        def __init__(self, user_id: int, guild: "DummyGuild") -> None:
            self.id = user_id
            self.guild = guild
            self.mention = f"<@{user_id}>"

    class DummyGuild:
        def __init__(self, guild_id: int) -> None:
            self.id = guild_id
            self._member: DummyMember | None = None

        def set_member(self, member: DummyMember) -> None:
            self._member = member

        def get_member(self, user_id: int) -> DummyMember | None:
            if self._member and self._member.id == user_id:
                return self._member
            return None

        async def fetch_member(self, user_id: int) -> DummyMember:
            member = self.get_member(user_id)
            if member is None:
                raise AssertionError("fetch_member should not be called")
            return member

    class DummyBot:
        def __init__(self, guild: DummyGuild) -> None:
            self._guild = guild

        def get_guild(self, guild_id: int) -> DummyGuild | None:
            if self._guild.id == guild_id:
                return self._guild
            return None

        async def fetch_guild(self, guild_id: int) -> DummyGuild:
            return self._guild

    store = CaptchaSessionStore()
    guild_id = 1122
    user_id = 3344
    dummy_guild = DummyGuild(guild_id)
    dummy_member = DummyMember(user_id, dummy_guild)
    dummy_guild.set_member(dummy_member)
    bot = DummyBot(dummy_guild)

    monkeypatch.setattr("modules.captcha.processor.discord.Member", DummyMember)

    processor = CaptchaCallbackProcessor(cast(commands.Bot, bot), store)

    async def fake_get_settings(guild: int, keys: list[str]) -> dict[str, Any]:
        return {
            "captcha-verification-enabled": True,
            "captcha-success-actions": None,
            "captcha-failure-actions": None,
            "captcha-max-attempts": 3,
            "captcha-log-channel": None,
            "captcha-delivery-method": "dm",
            "captcha-grace-period": None,
        }

    async def fake_log_to_channel(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr("modules.utils.mysql.get_settings", fake_get_settings)
    monkeypatch.setattr("modules.utils.mod_logging.log_to_channel", fake_log_to_channel)

    payload = CaptchaCallbackPayload.from_mapping(
        {
            "guildId": str(guild_id),
            "userId": str(user_id),
            "token": "dm-token",
            "status": "failed",
            "success": False,
            "failure_reason": "Incorrect answer.",
            "metadata": {
                "attemptsRemaining": 2,
                "maxAttempts": 3,
                "attempts": 1,
            },
        }
    )

    session = CaptchaSession(
        guild_id=guild_id,
        user_id=user_id,
        token="dm-token",
        expires_at=None,
        delivery_method="dm",
    )

    async def run() -> None:
        await store.put(session)
        result = await processor.process(payload)
        assert result.status == "failed"
        assert result.roles_applied == 0
        assert await store.get(guild_id, user_id) is session

    asyncio.run(run())


def test_captcha_failure_removes_session_when_attempts_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    class DummyMember:
        def __init__(self, user_id: int, guild: "DummyGuild") -> None:
            self.id = user_id
            self.guild = guild
            self.mention = f"<@{user_id}>"

    class DummyGuild:
        def __init__(self, guild_id: int) -> None:
            self.id = guild_id
            self._member: DummyMember | None = None

        def set_member(self, member: DummyMember) -> None:
            self._member = member

        def get_member(self, user_id: int) -> DummyMember | None:
            if self._member and self._member.id == user_id:
                return self._member
            return None

        async def fetch_member(self, user_id: int) -> DummyMember:
            member = self.get_member(user_id)
            if member is None:
                raise AssertionError("fetch_member should not be called")
            return member

    class DummyBot:
        def __init__(self, guild: DummyGuild) -> None:
            self._guild = guild

        def get_guild(self, guild_id: int) -> DummyGuild | None:
            if self._guild.id == guild_id:
                return self._guild
            return None

        async def fetch_guild(self, guild_id: int) -> DummyGuild:
            return self._guild

    store = CaptchaSessionStore()
    guild_id = 5566
    user_id = 7788
    dummy_guild = DummyGuild(guild_id)
    dummy_member = DummyMember(user_id, dummy_guild)
    dummy_guild.set_member(dummy_member)
    bot = DummyBot(dummy_guild)

    monkeypatch.setattr("modules.captcha.processor.discord.Member", DummyMember)

    processor = CaptchaCallbackProcessor(cast(commands.Bot, bot), store)

    async def fake_get_settings(guild: int, keys: list[str]) -> dict[str, Any]:
        return {
            "captcha-verification-enabled": True,
            "captcha-success-actions": None,
            "captcha-failure-actions": None,
            "captcha-max-attempts": 3,
            "captcha-log-channel": None,
            "captcha-delivery-method": "dm",
            "captcha-grace-period": None,
        }

    async def fake_log_to_channel(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr("modules.utils.mysql.get_settings", fake_get_settings)
    monkeypatch.setattr("modules.utils.mod_logging.log_to_channel", fake_log_to_channel)

    payload = CaptchaCallbackPayload.from_mapping(
        {
            "guildId": str(guild_id),
            "userId": str(user_id),
            "token": "dm-token",
            "status": "failed",
            "success": False,
            "failure_reason": "Incorrect answer.",
            "metadata": {
                "attemptsRemaining": 0,
                "maxAttempts": 3,
                "attempts": 3,
            },
        }
    )

    session = CaptchaSession(
        guild_id=guild_id,
        user_id=user_id,
        token="dm-token",
        expires_at=None,
        delivery_method="dm",
    )

    async def run() -> None:
        await store.put(session)
        result = await processor.process(payload)
        assert result.status == "failed"
        assert result.roles_applied == 0
        assert await store.get(guild_id, user_id) is None

    asyncio.run(run())


def test_vpn_policy_deny_applies_actions_and_records_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DummyMember:
        def __init__(self, user_id: int, guild: "DummyGuild") -> None:
            self.id = user_id
            self.guild = guild
            self.mention = f"<@{user_id}>"

    class DummyGuild:
        def __init__(self, guild_id: int) -> None:
            self.id = guild_id
            self._member: DummyMember | None = None

        def set_member(self, member: DummyMember) -> None:
            self._member = member

        def get_member(self, user_id: int) -> DummyMember | None:
            if self._member and self._member.id == user_id:
                return self._member
            return None

        async def fetch_member(self, user_id: int) -> DummyMember:
            member = self.get_member(user_id)
            if member is None:
                raise AssertionError("fetch_member should not be called")
            return member

    class DummyBot:
        def __init__(self, guild: DummyGuild) -> None:
            self._guild = guild

        def get_guild(self, guild_id: int) -> DummyGuild | None:
            if self._guild.id == guild_id:
                return self._guild
            return None

        async def fetch_guild(self, guild_id: int) -> DummyGuild:
            return self._guild

    store = CaptchaSessionStore()
    guild_id = 6001
    user_id = 7002
    dummy_guild = DummyGuild(guild_id)
    dummy_member = DummyMember(user_id, dummy_guild)
    dummy_guild.set_member(dummy_member)
    bot = DummyBot(dummy_guild)

    monkeypatch.setattr("modules.captcha.processor.discord.Member", DummyMember)

    processor = CaptchaCallbackProcessor(cast(commands.Bot, bot), store)

    async def fake_get_settings(guild: int, keys: list[str]) -> dict[str, Any]:
        return {
            "captcha-verification-enabled": True,
            "captcha-success-actions": None,
            "captcha-failure-actions": None,
            "captcha-max-attempts": 1,
            "captcha-log-channel": None,
            "captcha-delivery-method": "dm",
            "captcha-grace-period": None,
        }

    actions_called: list[list[str]] = []

    async def fake_perform_action(*args: Any, **kwargs: Any) -> None:
        action_string = kwargs.get("action_string")
        if isinstance(action_string, list):
            actions_called.append(list(action_string))
        elif isinstance(action_string, str):
            actions_called.append([action_string])

    async def fake_log_to_channel(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr("modules.utils.mysql.get_settings", fake_get_settings)
    monkeypatch.setattr(
        "modules.moderation.strike.perform_disciplinary_action",
        fake_perform_action,
    )
    monkeypatch.setattr("modules.utils.mod_logging.log_to_channel", fake_log_to_channel)

    payload_data = {
        "guildId": str(guild_id),
        "userId": str(user_id),
        "token": "vpn-token",
        "status": "failed",
        "success": False,
        "failure_reason": "vpn-detection",
        "metadata": {
            "policySource": "vpn-detection",
            "policyDetail": {
                "decision": "deny",
                "reason": "tor-exit",
                "riskScore": 98.5,
                "providersFlagged": 2,
                "providerCount": 2,
                "providers": [
                    {
                        "provider": "proxycheck",
                        "flagged": True,
                        "isVpn": True,
                        "isProxy": True,
                        "risk": 99,
                        "detail": {"ip": "1.1.1.1"},
                    },
                    {
                        "provider": "ipwhois",
                        "flagged": True,
                        "isVpn": True,
                        "isTor": True,
                        "risk": 95,
                    },
                ],
                "behavior": {
                    "failureCount": 1,
                    "accountAgeDays": 2,
                    "velocityScore": 4.2,
                },
                "actions": ["kick", "strike:vpn"],
                "timestamp": 1700000000000,
            },
        },
    }

    async def run() -> None:
        session = CaptchaSession(
            guild_id=guild_id,
            user_id=user_id,
            token="vpn-token",
            expires_at=None,
            delivery_method="dm",
        )
        await store.put(session)
        payload = CaptchaCallbackPayload.from_mapping(payload_data)
        result = await processor.process(payload, message_id="1-0")
        assert result.status == "failed"
        assert actions_called == [["kick", "strike:vpn"]]
        assert await store.get(guild_id, user_id) is None

        detection = session.metadata.get("vpn_detection")
        assert detection is not None
        assert detection["decision"] == "deny"
        assert detection["actions"] == ["kick", "strike:vpn"]
        assert detection["providers_flagged"] == 2
        assert detection["risk_score"] == pytest.approx(98.5)
        providers = detection.get("providers")
        assert isinstance(providers, list) and providers
        for provider in providers:
            assert set(provider.keys()).issubset(
                {"provider", "flagged", "isVpn", "isProxy", "isTor", "risk"}
            )
        assert isinstance(detection.get("flagged_at"), int)

        history = processor._vpn_action_history.get("vpn-token")
        assert history is not None and "1-0" in history

    asyncio.run(run())


def test_vpn_policy_actions_deduped_by_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DummyMember:
        def __init__(self, user_id: int, guild: "DummyGuild") -> None:
            self.id = user_id
            self.guild = guild
            self.mention = f"<@{user_id}>"

    class DummyGuild:
        def __init__(self, guild_id: int) -> None:
            self.id = guild_id
            self._member: DummyMember | None = None

        def set_member(self, member: DummyMember) -> None:
            self._member = member

        def get_member(self, user_id: int) -> DummyMember | None:
            if self._member and self._member.id == user_id:
                return self._member
            return None

        async def fetch_member(self, user_id: int) -> DummyMember:
            member = self.get_member(user_id)
            if member is None:
                raise AssertionError("fetch_member should not be called")
            return member

    class DummyBot:
        def __init__(self, guild: DummyGuild) -> None:
            self._guild = guild

        def get_guild(self, guild_id: int) -> DummyGuild | None:
            if self._guild.id == guild_id:
                return self._guild
            return None

        async def fetch_guild(self, guild_id: int) -> DummyGuild:
            return self._guild

    store = CaptchaSessionStore()
    guild_id = 6100
    user_id = 7200
    dummy_guild = DummyGuild(guild_id)
    dummy_member = DummyMember(user_id, dummy_guild)
    dummy_guild.set_member(dummy_member)
    bot = DummyBot(dummy_guild)

    monkeypatch.setattr("modules.captcha.processor.discord.Member", DummyMember)

    processor = CaptchaCallbackProcessor(cast(commands.Bot, bot), store)

    async def fake_get_settings(guild: int, keys: list[str]) -> dict[str, Any]:
        return {
            "captcha-verification-enabled": True,
            "captcha-success-actions": None,
            "captcha-failure-actions": None,
            "captcha-max-attempts": 1,
            "captcha-log-channel": None,
            "captcha-delivery-method": "dm",
            "captcha-grace-period": None,
        }

    action_calls = 0

    async def fake_perform_action(*args: Any, **kwargs: Any) -> None:
        nonlocal action_calls
        action_calls += 1

    async def fake_log_to_channel(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr("modules.utils.mysql.get_settings", fake_get_settings)
    monkeypatch.setattr(
        "modules.moderation.strike.perform_disciplinary_action",
        fake_perform_action,
    )
    monkeypatch.setattr("modules.utils.mod_logging.log_to_channel", fake_log_to_channel)

    payload_template = {
        "guildId": str(guild_id),
        "userId": str(user_id),
        "token": "vpn-token",
        "status": "failed",
        "success": False,
        "metadata": {
            "policySource": "vpn-detection",
            "policyDetail": {
                "decision": "deny",
                "actions": ["kick"],
            },
        },
    }

    async def run() -> None:
        first_session = CaptchaSession(
            guild_id=guild_id,
            user_id=user_id,
            token="vpn-token",
            expires_at=None,
            delivery_method="dm",
        )
        await store.put(first_session)
        first_payload = CaptchaCallbackPayload.from_mapping(payload_template)
        await processor.process(first_payload, message_id="9-1")
        assert action_calls == 1

        second_session = CaptchaSession(
            guild_id=guild_id,
            user_id=user_id,
            token="vpn-token",
            expires_at=None,
            delivery_method="dm",
        )
        await store.put(second_session)
        second_payload = CaptchaCallbackPayload.from_mapping(payload_template)
        await processor.process(second_payload, message_id="9-1")
        assert action_calls == 1

    asyncio.run(run())


def test_vpn_policy_challenge_keeps_session(monkeypatch: pytest.MonkeyPatch) -> None:
    class DummyMember:
        def __init__(self, user_id: int, guild: "DummyGuild") -> None:
            self.id = user_id
            self.guild = guild
            self.mention = f"<@{user_id}>"

    class DummyGuild:
        def __init__(self, guild_id: int) -> None:
            self.id = guild_id
            self._member: DummyMember | None = None

        def set_member(self, member: DummyMember) -> None:
            self._member = member

        def get_member(self, user_id: int) -> DummyMember | None:
            if self._member and self._member.id == user_id:
                return self._member
            return None

        async def fetch_member(self, user_id: int) -> DummyMember:
            member = self.get_member(user_id)
            if member is None:
                raise AssertionError("fetch_member should not be called")
            return member

    class DummyBot:
        def __init__(self, guild: DummyGuild) -> None:
            self._guild = guild

        def get_guild(self, guild_id: int) -> DummyGuild | None:
            if self._guild.id == guild_id:
                return self._guild
            return None

        async def fetch_guild(self, guild_id: int) -> DummyGuild:
            return self._guild

    store = CaptchaSessionStore()
    guild_id = 6300
    user_id = 8100
    dummy_guild = DummyGuild(guild_id)
    dummy_member = DummyMember(user_id, dummy_guild)
    dummy_guild.set_member(dummy_member)
    bot = DummyBot(dummy_guild)

    monkeypatch.setattr("modules.captcha.processor.discord.Member", DummyMember)

    processor = CaptchaCallbackProcessor(cast(commands.Bot, bot), store)

    async def fake_get_settings(guild: int, keys: list[str]) -> dict[str, Any]:
        return {
            "captcha-verification-enabled": True,
            "captcha-success-actions": None,
            "captcha-failure-actions": None,
            "captcha-max-attempts": 3,
            "captcha-log-channel": None,
            "captcha-delivery-method": "dm",
            "captcha-grace-period": None,
        }

    async def fake_perform_action(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("No disciplinary action should be applied")

    async def fake_log_to_channel(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr("modules.utils.mysql.get_settings", fake_get_settings)
    monkeypatch.setattr(
        "modules.moderation.strike.perform_disciplinary_action",
        fake_perform_action,
    )
    monkeypatch.setattr("modules.utils.mod_logging.log_to_channel", fake_log_to_channel)

    payload = CaptchaCallbackPayload.from_mapping(
        {
            "guildId": str(guild_id),
            "userId": str(user_id),
            "token": "vpn-token",
            "status": "failed",
            "success": False,
            "metadata": {
                "policySource": "vpn-detection",
                "policyDetail": {
                    "decision": "challenge",
                    "actions": ["challenge"],
                    "escalation": "secondary-verification",
                    "cachedState": "fresh",
                },
            },
        }
    )

    session = CaptchaSession(
        guild_id=guild_id,
        user_id=user_id,
        token="vpn-token",
        expires_at=None,
        delivery_method="dm",
    )

    async def run() -> None:
        await store.put(session)
        result = await processor.process(payload, message_id="3-2")
        assert result.status == "failed"
        assert await store.get(guild_id, user_id) is session
        detection = session.metadata.get("vpn_detection")
        assert detection is not None
        assert detection["decision"] == "challenge"
        assert detection.get("escalation") == "secondary-verification"

    asyncio.run(run())

def test_normalize_xautoclaim_response_handles_extended_list() -> None:
    response = [
        "0-2",
        [("1-0", {"payload": "data"})],
        ["deleted-id"],
    ]
    next_id, messages = CaptchaStreamListener._normalize_xautoclaim_response(response)
    assert next_id == "0-2"
    assert messages == response[1]

def test_normalize_xautoclaim_response_handles_response_object() -> None:
    class DummyResponse:
        def __init__(self) -> None:
            self.next_start_id = "5-0"
            self.messages = [("2-0", {"payload": "data"})]
            self.deleted_ids = ["1-0"]

    dummy = DummyResponse()
    next_id, messages = CaptchaStreamListener._normalize_xautoclaim_response(dummy)
    assert next_id == "5-0"
    assert messages == dummy.messages

def test_normalize_xautoclaim_response_rejects_unknown_shape() -> None:
    class UnknownResponse:
        pass

    with pytest.raises(TypeError):
        CaptchaStreamListener._normalize_xautoclaim_response(UnknownResponse())

def test_normalize_xautoclaim_response_rejects_short_sequence() -> None:
    with pytest.raises(ValueError):
        CaptchaStreamListener._normalize_xautoclaim_response(["only-one-element"])
