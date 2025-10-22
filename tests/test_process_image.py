import importlib
import importlib.util
import sys
import types
from pathlib import Path

import pytest
from PIL import Image

project_root = Path(__file__).resolve().parents[1]
sys.path.append(str(project_root))

# Stub cogs.nsfw to avoid heavy Discord dependencies.
nsfw_stub = types.ModuleType("cogs.nsfw")
nsfw_stub.NSFW_CATEGORY_SETTING = "nsfw-detection-categories"
sys.modules["cogs.nsfw"] = nsfw_stub

# Stub cv2 to avoid importing OpenCV in tests.
cv2_stub = types.ModuleType("cv2")


class _DummyCapture:
    def __init__(self, *_args, **_kwargs):
        pass

    def get(self, *_args, **_kwargs):
        return 0

    def read(self):
        return False, None

    def isOpened(self):
        return False

    def release(self):
        pass


def _imwrite(*_args, **_kwargs):
    return True


cv2_stub.VideoCapture = _DummyCapture
cv2_stub.CAP_PROP_FRAME_COUNT = 0
cv2_stub.IMWRITE_JPEG_QUALITY = 95
cv2_stub.imwrite = _imwrite
sys.modules["cv2"] = cv2_stub

filetype_stub = types.ModuleType("filetype")


def _guess(_filename):
    return None


filetype_stub.guess = _guess
sys.modules["filetype"] = filetype_stub

# Provide stubs before importing the module to avoid heavy dependencies.
modules_pkg = importlib.import_module("modules")
utils_pkg = importlib.import_module("modules.utils")

clip_vectors_stub = types.ModuleType("modules.utils.clip_vectors")
clip_vectors_stub._query_calls = 0


def _query_similar(image, threshold=0):
    clip_vectors_stub._query_calls += 1
    return []


async def _delete_vectors(_vector_ids):
    return None


clip_vectors_stub.query_similar = _query_similar
clip_vectors_stub.delete_vectors = _delete_vectors
clip_vectors_stub.is_available = lambda: False
clip_vectors_stub.register_failure_callback = lambda *_args, **_kwargs: None
sys.modules["modules.utils.clip_vectors"] = clip_vectors_stub
setattr(utils_pkg, "clip_vectors", clip_vectors_stub)

mysql_stub = types.ModuleType("modules.utils.mysql")


async def _get_settings(_guild_id, _keys):
    return {  # Mirrors expected keys
        "nsfw-high-accuracy": False,
        "threshold": 0.7,
        nsfw_stub.NSFW_CATEGORY_SETTING: [],
    }


async def _is_accelerated(*_args, **_kwargs):
    return False


mysql_stub.get_settings = _get_settings
mysql_stub.is_accelerated = _is_accelerated
sys.modules["modules.utils.mysql"] = mysql_stub
setattr(utils_pkg, "mysql", mysql_stub)

moderation_stub = types.ModuleType("modules.nsfw_scanner.helpers.moderation")


async def _moderator_api(*_args, **_kwargs):
    return {
        "is_nsfw": False,
        "reason": "openai_moderation",
    }


moderation_stub.moderator_api = _moderator_api
sys.modules["modules.nsfw_scanner.helpers.moderation"] = moderation_stub

images_spec = importlib.util.spec_from_file_location(
    "modules.nsfw_scanner.helpers.images",
    project_root / "modules" / "nsfw_scanner" / "helpers" / "images.py",
)
images_mod = importlib.util.module_from_spec(images_spec)
assert images_spec.loader is not None
images_spec.loader.exec_module(images_mod)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_process_image_reuses_png_without_conversion(monkeypatch, tmp_path):
    png_path = tmp_path / "sample.png"
    Image.new("RGBA", (8, 8), (255, 0, 0, 255)).save(png_path)

    def fail_convert(*_args, **_kwargs):
        raise AssertionError("_encode_image_to_png_bytes should not be called for PNG input")

    monkeypatch.setattr(images_mod, "_encode_image_to_png_bytes", fail_convert)

    async def fake_get_settings(_guild_id, _keys):
        return {
            images_mod.NSFW_CATEGORY_SETTING: [],
            "nsfw-high-accuracy": False,
            "threshold": 0.7,
        }

    monkeypatch.setattr(images_mod.mysql, "get_settings", fake_get_settings)

    async def fake_is_accelerated(**_kwargs):
        return False

    monkeypatch.setattr(images_mod.mysql, "is_accelerated", fake_is_accelerated)

    result = await images_mod.process_image(
        scanner=types.SimpleNamespace(bot=None),
        original_filename=str(png_path),
        guild_id=123,
        clean_up=False,
    )

    assert result is not None
    assert result.get("is_nsfw") is False


@pytest.mark.anyio
async def test_process_image_converts_non_png(monkeypatch, tmp_path):
    jpeg_path = tmp_path / "sample.jpg"
    Image.new("RGB", (8, 8), (0, 255, 0)).save(jpeg_path, format="JPEG")

    original_convert = images_mod._encode_image_to_png_bytes
    convert_calls = {"count": 0}

    def tracked_convert(src):
        convert_calls["count"] += 1
        return original_convert(src)

    monkeypatch.setattr(images_mod, "_encode_image_to_png_bytes", tracked_convert)

    async def fake_get_settings(_guild_id, _keys):
        return {
            images_mod.NSFW_CATEGORY_SETTING: [],
            "nsfw-high-accuracy": False,
            "threshold": 0.7,
        }

    async def fake_is_accelerated(**_kwargs):
        return False

    monkeypatch.setattr(images_mod.mysql, "get_settings", fake_get_settings)
    monkeypatch.setattr(images_mod.mysql, "is_accelerated", fake_is_accelerated)

    result = await images_mod.process_image(
        scanner=types.SimpleNamespace(bot=None),
        original_filename=str(jpeg_path),
        guild_id=456,
        clean_up=False,
    )

    assert result is not None
    assert result.get("is_nsfw") is False
    assert convert_calls["count"] == 1


@pytest.mark.anyio
async def test_process_image_uses_precomputed_settings(monkeypatch, tmp_path):
    png_path = tmp_path / "precomputed.png"
    Image.new("RGBA", (4, 4), (0, 0, 255, 255)).save(png_path)

    async def fail_get_settings(*_args, **_kwargs):
        raise AssertionError("get_settings should not be called when settings provided")

    async def fail_is_accelerated(**_kwargs):
        raise AssertionError("is_accelerated should not be called when accelerated provided")

    monkeypatch.setattr(images_mod.mysql, "get_settings", fail_get_settings)
    monkeypatch.setattr(images_mod.mysql, "is_accelerated", fail_is_accelerated)

    settings = {
        images_mod.NSFW_CATEGORY_SETTING: [],
        "nsfw-high-accuracy": False,
        "threshold": 0.7,
    }

    result = await images_mod.process_image(
        scanner=types.SimpleNamespace(bot=None),
        original_filename=str(png_path),
        guild_id=789,
        clean_up=False,
        settings=settings,
        accelerated=False,
    )

    assert result is not None
    assert result.get("is_nsfw") is False
