import json
import os
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

os.environ.setdefault("FERNET_SECRET_KEY", Fernet.generate_key().decode())

from modules.nsfw_scanner.custom_blocks.config import CustomBlockStreamConfig
from modules.nsfw_scanner.custom_blocks.service import CustomBlockError
from modules.nsfw_scanner.custom_blocks.stream import CustomBlockStreamProcessor
from modules.utils.redis_stream import RedisStreamMessage


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def stream_processor():
    config = CustomBlockStreamConfig(
        enabled=True,
        redis_url="redis://localhost/0",
        stream="nsfw:custom-blocks:commands",
        group="test-group",
        consumer_name="test-consumer",
    )
    return CustomBlockStreamProcessor(bot=None, config=config)


@pytest.mark.anyio
async def test_delete_payload_accepts_camelcase_ids(monkeypatch, stream_processor):
    calls: dict[str, tuple[int, int]] = {}

    async def _fake_delete(guild_id: int, vector_id: int):
        calls["args"] = (guild_id, vector_id)
        return {"label": "Test label"}

    monkeypatch.setattr(
        "modules.nsfw_scanner.custom_blocks.stream.delete_custom_block",
        _fake_delete,
    )

    payload = {
        "action": "delete",
        "guildId": "12345",
        "vectorId": "6789",
        "requestId": "abc",
    }

    response = await stream_processor._handle_payload(payload)

    assert response == {
        "request_id": "abc",
        "status": "ok",
        "action": "delete",
        "guild_id": "12345",
        "vector_id": "6789",
        "label": "Test label",
    }
    assert calls["args"] == (12345, 6789)


@pytest.mark.anyio
async def test_delete_payload_accepts_mixed_field_names(monkeypatch, stream_processor):
    calls: dict[str, tuple[int, int]] = {}

    async def _fake_delete(guild_id: int, vector_id: int):
        calls["args"] = (guild_id, vector_id)
        return {"label": "Test label", "not_found": True}

    monkeypatch.setattr(
        "modules.nsfw_scanner.custom_blocks.stream.delete_custom_block",
        _fake_delete,
    )

    payload = {
        "action": "delete",
        "guild_id": "98765",
        "vectorId": "4321",
    }

    response = await stream_processor._handle_payload(payload)

    assert response["not_found"] == "true"
    assert response["guild_id"] == "98765"
    assert response["vector_id"] == "4321"
    assert calls["args"] == (98765, 4321)


@pytest.mark.anyio
async def test_delete_payload_still_requires_ids(monkeypatch, stream_processor):
    def _unexpected_delete(*_args, **_kwargs):
        raise AssertionError("delete_custom_block should not be called when ids are missing")

    monkeypatch.setattr(
        "modules.nsfw_scanner.custom_blocks.stream.delete_custom_block",
        _unexpected_delete,
    )

    payload = {
        "action": "delete",
    }

    with pytest.raises(CustomBlockError) as excinfo:
        await stream_processor._handle_payload(payload)

    assert "guild_id is required" in str(excinfo.value)


@pytest.mark.anyio
async def test_list_payload_serializes_ids_as_strings(monkeypatch, stream_processor):
    vector_id = 462238347336331700
    uploader_id = 123456789012345678

    async def _fake_list(_guild_id: int):
        return [
            {
                "vector_id": vector_id,
                "label": "Sample",
                "uploaded_by": uploader_id,
                "uploaded_at": 1_700_000_000,
                "source": "dashboard",
            }
        ]

    monkeypatch.setattr(
        "modules.nsfw_scanner.custom_blocks.stream.list_custom_blocks",
        _fake_list,
    )

    payload = {
        "action": "list",
        "guild_id": "42",
    }

    response = await stream_processor._handle_payload(payload)
    entries = json.loads(response["entries"])
    assert len(entries) == 1
    entry = entries[0]
    assert entry["vector_id"] == str(vector_id)
    assert entry["uploaded_by"] == str(uploader_id)


@pytest.mark.anyio
async def test_handle_message_reports_failures_for_all_actions(monkeypatch, stream_processor):
    async def _boom(self, _payload):
        raise CustomBlockError("explode")

    reported: list[dict[str, object]] = []

    async def _fake_report(self, *, payload, error, message_id):
        reported.append(
            {
                "payload": payload,
                "error": error,
                "message_id": message_id,
            }
        )

    monkeypatch.setattr(
        CustomBlockStreamProcessor,
        "_handle_payload",
        _boom,
    )
    monkeypatch.setattr(
        CustomBlockStreamProcessor,
        "_report_command_failure",
        _fake_report,
    )

    message = RedisStreamMessage(
        stream="nsfw:custom-blocks:commands",
        message_id="123-0",
        fields={
            "action": "add",
            "guild_id": "555",
            "vector_id": "999",
        },
    )

    await stream_processor.handle_message(message)

    assert reported, "failure reporter should be invoked for non-delete actions"
    report = reported[0]
    assert isinstance(report["error"], CustomBlockError)
    assert report["payload"]["action"] == "add"
