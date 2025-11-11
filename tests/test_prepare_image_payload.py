import base64
import os
import sys
from pathlib import Path

from PIL import Image


project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.append(str(project_root))

os.environ.setdefault(
    "FERNET_SECRET_KEY",
    base64.urlsafe_b64encode(b"0" * 32).decode(),
)

from modules.nsfw_scanner.helpers.payloads import prepare_image_payload_sync


def test_prepare_image_payload_preserves_dimensions_when_limit_disabled():
    image = Image.new("RGB", (5000, 3000), color=(255, 255, 255))

    prepared = prepare_image_payload_sync(
        image=image,
        image_bytes=None,
        image_path=None,
        image_mime="image/jpeg",
        original_size=5_000_000,
        max_image_edge=0,
        jpeg_target_bytes=None,
        target_format="jpeg",
    )

    assert prepared.width == 5000
    assert prepared.height == 3000
    assert prepared.resized is False
    assert prepared.edge_limit is None


def test_prepare_image_payload_resizes_when_limit_specified():
    image = Image.new("RGB", (5000, 3000), color=(255, 255, 255))

    prepared = prepare_image_payload_sync(
        image=image,
        image_bytes=None,
        image_path=None,
        image_mime="image/jpeg",
        original_size=5_000_000,
        max_image_edge=1024,
        jpeg_target_bytes=None,
        target_format="jpeg",
    )

    assert prepared.resized is True
    assert prepared.edge_limit == 1024
    assert prepared.width == 1024
    expected_height = max(1, int(round(3000 * (1024 / 5000))))
    assert prepared.height == expected_height
