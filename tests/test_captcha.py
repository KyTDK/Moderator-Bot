from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("FERNET_SECRET_KEY", "DeJ3sXDDTTbikeRSJzRgg8r_Ch61_NbE8D3LWnLOJO4=")
sys.path.append(str(Path(__file__).resolve().parents[1]))

from modules.captcha.models import CaptchaCallbackPayload, CaptchaPayloadError
from modules.captcha.sessions import CaptchaSession, CaptchaSessionStore

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
