from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from cryptography.fernet import Fernet

os.environ.setdefault("FERNET_SECRET_KEY", Fernet.generate_key().decode())

sys.path.append(str(Path(__file__).resolve().parents[1]))

import modules.nsfw_scanner.scanner.media_collector as media_collector  # noqa: E402


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _DummyAttachment:
    def __init__(self, *, filename: str, url: str, proxy_url: str | None = None):
        self.filename = filename
        self.url = url
        self.proxy_url = proxy_url
        self.size = 123
        self.id = 987
        self.hash = "dummy-hash"


class _DummyMessage(SimpleNamespace):
    def __init__(self, **kwargs):
        defaults = {
            "attachments": [],
            "embeds": [],
            "stickers": [],
            "message_snapshots": [],
            "id": 100,
            "channel": SimpleNamespace(id=200),
            "guild": SimpleNamespace(id=300),
        }
        defaults.update(kwargs)
        super().__init__(**defaults)


async def test_collect_media_items_without_hydration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wait_called = False

    async def fake_wait_for_hydration(message):  # pragma: no cover - simple stub
        nonlocal wait_called
        wait_called = True
        return message

    monkeypatch.setattr(media_collector, "wait_for_hydration", fake_wait_for_hydration)

    message = _DummyMessage(
        attachments=[
            _DummyAttachment(
                filename="image.png",
                url="https://cdn.example.com/image.png",
            )
        ]
    )

    context = SimpleNamespace(tenor_allowed=True)

    items = await media_collector.collect_media_items(message, bot=None, context=context)

    assert not wait_called
    assert items
    metadata = items[0].metadata
    assert "hydration_stage" not in metadata


async def test_collect_media_items_with_hydration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hydrated_attachment = _DummyAttachment(
        filename="hydrated.png",
        url="https://cdn.example.com/hydrated.png",
    )

    hydrated_message = _DummyMessage(attachments=[hydrated_attachment])

    async def fake_wait_for_hydration(message):  # pragma: no cover - simple stub
        return hydrated_message

    monkeypatch.setattr(media_collector, "wait_for_hydration", fake_wait_for_hydration)

    message = _DummyMessage()
    context = SimpleNamespace(tenor_allowed=True)

    items = await media_collector.collect_media_items(message, bot=None, context=context)

    assert items
    metadata = items[0].metadata

    assert metadata.get("hydration_stage") == "raw_message_update"
    assert metadata.get("hydration_status") == "hydrated"
    assert metadata.get("hydration_method") == "wait_for_hydration"
    assert metadata.get("hydration_origin") == "nsfw_scanner.collect_media_items"
    assert metadata.get("hydration_attempts") == 1
    assert metadata.get("hydrated_urls") == ["https://cdn.example.com/hydrated.png"]
    assert "hydration_elapsed_ms" in metadata
