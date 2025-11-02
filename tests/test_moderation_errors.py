from __future__ import annotations

import os
import sys
from pathlib import Path

from cryptography.fernet import Fernet

project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.append(str(project_root))

os.environ.setdefault("FERNET_SECRET_KEY", Fernet.generate_key().decode())

from modules.nsfw_scanner.helpers.moderation_errors import build_error_context


class DummyException(Exception):
    """Simple exception type for testing."""


def test_build_error_context_includes_discord_metadata() -> None:
    exc = DummyException("boom")
    payload_metadata = {
        "guild_id": 123456789012345678,
        "channel_id": 987654321098765432,
        "author_id": 111222333444555666,
        "user_id": 111222333444555666,
        "message_id": 555444333222111000,
        "message_jump_url": "https://discord.com/channels/123456789012345678/987654321098765432/555444333222111000",
        "source_url": "https://cdn.discordapp.com/attachments/123/456/image.png",
    }

    context = build_error_context(
        exc=exc,
        attempt_number=1,
        max_attempts=2,
        request_model="omni-moderation-latest",
        has_image_input=True,
        image_state=None,
        payload_metadata=payload_metadata,
    )

    assert "guild_id=123456789012345678" in context
    assert "channel_id=987654321098765432" in context
    assert "user_id=111222333444555666" in context
    assert "message_id=555444333222111000" in context
    assert (
        "message_jump_url=https://discord.com/channels/123456789012345678/987654321098765432/555444333222111000"
        in context
    )
    assert "source_url=https://cdn.discordapp.com/attachments/123/456/image.png" in context
    assert "payload_source_host=cdn.discordapp.com" in context
