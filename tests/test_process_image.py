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
nsfw_stub.NSFW_TEXT_CATEGORY_SETTING = "nsfw-text-categories"
nsfw_stub.NSFW_TEXT_ENABLED_SETTING = "nsfw-text-enabled"
nsfw_stub.NSFW_TEXT_THRESHOLD_SETTING = "nsfw-text-threshold"
nsfw_stub.NSFW_TEXT_ACTION_SETTING = "nsfw-text-action"
nsfw_stub.NSFW_TEXT_STRIKES_ONLY_SETTING = "nsfw-text-strikes-only"
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

log_channel_stub = types.ModuleType("modules.utils.log_channel")


async def _send_log_message(*_args, **_kwargs):
    return False


log_channel_stub.send_log_message = _send_log_message
log_channel_stub.log_serious_issue = _send_log_message
sys.modules["modules.utils.log_channel"] = log_channel_stub

mod_logging_stub = types.ModuleType("modules.utils.mod_logging")


async def _log_to_channel(*_args, **_kwargs):
    return None


mod_logging_stub.log_to_channel = _log_to_channel
sys.modules["modules.utils.mod_logging"] = mod_logging_stub

discord_stub = types.ModuleType("discord")


class _DummyLocale(str):
    __slots__ = ("value",)

    def __new__(cls, value: str) -> "_DummyLocale":
        instance = str.__new__(cls, value)
        instance.value = value
        return instance


for _name, _value in {
    "english_us": "en-US",
    "english_gb": "en-GB",
    "french": "fr",
    "spanish": "es",
    "polish": "pl",
    "portuguese_brazil": "pt-BR",
    "portuguese": "pt",
    "russian": "ru",
    "swedish": "sv",
    "vietnamese": "vi",
    "chinese_simplified": "zh-CN",
}.items():
    setattr(_DummyLocale, _name, _DummyLocale(_value))


discord_stub.Locale = _DummyLocale


class _DummyEmbed:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def add_field(self, *args, **kwargs):
        return None

    def set_thumbnail(self, *args, **kwargs):
        return None

    def set_image(self, *args, **kwargs):
        return None


class _DummyColor:
    @staticmethod
    def orange():
        return 0xFFA500

    @staticmethod
    def red():
        return 0xFF0000

    @staticmethod
    def dark_grey():
        return 0x2F3136


class _DummyFile:
    def __init__(self, *args, **kwargs):
        pass


class _DummyAllowedMentions:
    def __init__(self, *args, **kwargs):
        pass

    @staticmethod
    def none():
        return _DummyAllowedMentions()


class _DummyClient:
    pass


discord_stub.Embed = _DummyEmbed
discord_stub.Color = _DummyColor
discord_stub.File = _DummyFile
discord_stub.AllowedMentions = _DummyAllowedMentions
discord_stub.Client = _DummyClient
discord_stub.abc = types.SimpleNamespace(Messageable=object)
discord_stub.Forbidden = type("Forbidden", (Exception,), {})
discord_stub.HTTPException = type("HTTPException", (Exception,), {})
sys.modules["discord"] = discord_stub

discord_ext_stub = types.ModuleType("discord.ext")
commands_stub = types.ModuleType("discord.ext.commands")
tasks_stub = types.ModuleType("discord.ext.tasks")


def _loop(*_args, **_kwargs):
    def _decorator(func):
        return func

    return _decorator


tasks_stub.loop = _loop


class _DummyBot:
    pass


class _DummyCog:
    def __init__(self, *args, **_kwargs):
        pass


def _identity_decorator(*_args, **_kwargs):
    def _wrap(func):
        return func

    return _wrap


commands_stub.Bot = _DummyBot
commands_stub.Cog = _DummyCog
commands_stub.AutoShardedBot = _DummyBot
commands_stub.command = _identity_decorator
commands_stub.Cog.listener = staticmethod(_identity_decorator)
discord_ext_stub.commands = commands_stub
discord_ext_stub.tasks = tasks_stub

sys.modules["discord.ext"] = discord_ext_stub
sys.modules["discord.ext.commands"] = commands_stub
sys.modules["discord.ext.tasks"] = tasks_stub

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
    _kwargs.pop("latency_callback", None)
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
context_mod = importlib.import_module("modules.nsfw_scanner.helpers.context")


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
    monkeypatch.setattr(context_mod.mysql, "get_settings", fake_get_settings)

    async def fake_is_accelerated(**_kwargs):
        return False

    monkeypatch.setattr(images_mod.mysql, "is_accelerated", fake_is_accelerated)
    monkeypatch.setattr(context_mod.mysql, "is_accelerated", fake_is_accelerated)

    result = await images_mod.process_image(
        scanner=types.SimpleNamespace(bot=None),
        original_filename=str(png_path),
        guild_id=123,
        clean_up=False,
    )

    assert result is not None
    assert result.get("is_nsfw") is False


@pytest.mark.anyio
async def test_process_image_reuses_jpeg_when_possible(monkeypatch, tmp_path):
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
    monkeypatch.setattr(context_mod.mysql, "get_settings", fake_get_settings)
    monkeypatch.setattr(images_mod.mysql, "is_accelerated", fake_is_accelerated)
    monkeypatch.setattr(context_mod.mysql, "is_accelerated", fake_is_accelerated)

    result = await images_mod.process_image(
        scanner=types.SimpleNamespace(bot=None),
        original_filename=str(jpeg_path),
        guild_id=456,
        clean_up=False,
    )

    assert result is not None
    assert result.get("is_nsfw") is False
    assert convert_calls["count"] == 0


@pytest.mark.anyio
async def test_process_image_converts_unsupported_formats(monkeypatch, tmp_path):
    bmp_path = tmp_path / "sample.bmp"
    Image.new("RGB", (8, 8), (0, 0, 255)).save(bmp_path, format="BMP")

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
    monkeypatch.setattr(context_mod.mysql, "get_settings", fake_get_settings)
    monkeypatch.setattr(images_mod.mysql, "is_accelerated", fake_is_accelerated)
    monkeypatch.setattr(context_mod.mysql, "is_accelerated", fake_is_accelerated)

    result = await images_mod.process_image(
        scanner=types.SimpleNamespace(bot=None),
        original_filename=str(bmp_path),
        guild_id=321,
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
