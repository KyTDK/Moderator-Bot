import base64
import os
import sys
from pathlib import Path

import pytest


project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.append(str(project_root))

os.environ.setdefault(
    "FERNET_SECRET_KEY",
    base64.urlsafe_b64encode(b"0" * 32).decode(),
)

from modules.nsfw_scanner.helpers.latency import ModeratorLatencyTracker
from modules.nsfw_scanner.helpers.moderation_state import ImageModerationState
from modules.nsfw_scanner.helpers.payloads import PreparedImagePayload


def _make_tracker():
    return ModeratorLatencyTracker()


def test_image_moderation_state_allows_remote_for_passthrough_payload():
    tracker = _make_tracker()
    metadata: dict[str, object] = {}
    prepared = PreparedImagePayload(
        data=b"jpeg-bytes",
        mime="image/jpeg",
        width=12,
        height=8,
        resized=False,
        strategy="passthrough",
        quality=None,
        original_mime="image/jpeg",
    )

    state = ImageModerationState.from_prepared_payload(
        prepared,
        latency_tracker=tracker,
        payload_metadata=metadata,
        source_url="https://cdn.example.com/image.jpg",
        quality_label=None,
    )

    assert state.use_remote is True
    inputs = state.build_inputs(tracker)
    assert inputs == [
        {
            "type": "image_url",
            "image_url": {"url": "https://cdn.example.com/image.jpg"},
        }
    ]
    assert metadata.get("moderation_payload_strategy") == "passthrough"


@pytest.mark.parametrize(
    ("strategy", "mime", "quality"),
    [
        ("converted_png", "image/png", None),
        ("compressed_jpeg", "image/jpeg", 72),
    ],
)
def test_image_moderation_state_uses_inline_payload_for_converted_images(strategy, mime, quality):
    tracker = _make_tracker()
    metadata: dict[str, object] = {}
    prepared = PreparedImagePayload(
        data=b"converted-bytes",
        mime=mime,
        width=10,
        height=10,
        resized=True,
        strategy=strategy,
        quality=quality,
        original_mime="image/jpeg",
    )

    state = ImageModerationState.from_prepared_payload(
        prepared,
        latency_tracker=tracker,
        payload_metadata=metadata,
        source_url="https://media.tenor.com/funSJ9kS9akAAAPo/saweetie-twerk.mp4",
        quality_label=None,
    )

    assert state.use_remote is False
    assert state.remote_disabled is False
    inputs = state.build_inputs(tracker)
    assert inputs and isinstance(inputs, list)
    inline_url = inputs[0]["image_url"]["url"]
    assert inline_url.startswith(f"data:{mime};base64,")
    assert metadata.get("moderation_payload_strategy") == strategy
