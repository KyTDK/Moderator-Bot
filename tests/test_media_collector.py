import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace

project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.append(str(project_root))

if "discord" not in sys.modules:
    discord_stub = types.ModuleType("discord")

    class _DummyColor:
        def __init__(self, value: int = 0):
            self.value = value

        @classmethod
        def red(cls):
            return cls(0xFF0000)

        @classmethod
        def orange(cls):
            return cls(0xFFA500)

        @classmethod
        def dark_grey(cls):
            return cls(0x2F3136)

    class _DummyEmbed:
        def __init__(self, *args, **kwargs):
            self.fields = []

        def add_field(self, *args, **kwargs):
            self.fields.append((args, kwargs))

        def set_footer(self, *args, **kwargs):
            pass

        def set_thumbnail(self, *args, **kwargs):
            pass

    class _DummyFile:
        def __init__(self, *args, **kwargs):
            pass

    discord_stub.Color = _DummyColor
    discord_stub.Embed = _DummyEmbed
    discord_stub.File = _DummyFile

    errors_stub = types.ModuleType("discord.errors")
    errors_stub.NotFound = type("NotFound", (Exception,), {})

    ext_stub = types.ModuleType("discord.ext")
    commands_stub = types.ModuleType("discord.ext.commands")

    class _DummyCog:
        @staticmethod
        def listener(*_args, **_kwargs):
            def decorator(func):
                return func

            return decorator

    class _DummyBot:
        pass

    commands_stub.Cog = _DummyCog
    commands_stub.Bot = _DummyBot

    ext_stub.commands = commands_stub
    discord_stub.errors = errors_stub
    discord_stub.ext = ext_stub

    sys.modules["discord"] = discord_stub
    sys.modules["discord.errors"] = errors_stub
    sys.modules["discord.ext"] = ext_stub
    sys.modules["discord.ext.commands"] = commands_stub

if "pillow_avif" not in sys.modules:
    sys.modules["pillow_avif"] = types.ModuleType("pillow_avif")

if "cogs.nsfw" not in sys.modules:
    nsfw_stub = types.ModuleType("cogs.nsfw")
    nsfw_stub.NSFW_CATEGORY_SETTING = "nsfw-detection-categories"
    sys.modules["cogs.nsfw"] = nsfw_stub

if "cogs.hydration" not in sys.modules:
    hydration_stub = types.ModuleType("cogs.hydration")

    async def _wait_for_hydration(message):
        return message

    hydration_stub.wait_for_hydration = _wait_for_hydration
    sys.modules["cogs.hydration"] = hydration_stub

discord_utils_stub = types.ModuleType("modules.utils.discord_utils")


async def _async_noop(*_args, **_kwargs):
    return None


async def _async_true(*_args, **_kwargs):
    return True


def _sync_list(*_args, **_kwargs):
    return []


discord_utils_stub.safe_get_channel = _async_noop
discord_utils_stub.safe_get_user = _async_noop
discord_utils_stub.safe_get_member = _async_noop
discord_utils_stub.safe_get_message = _async_noop
discord_utils_stub.ensure_member_with_presence = _async_noop
discord_utils_stub.message_user = _async_noop
discord_utils_stub.require_accelerated = _async_true
discord_utils_stub.resolve_role_references = _sync_list
sys.modules.setdefault("modules.utils.discord_utils", discord_utils_stub)

images_stub = types.ModuleType("modules.nsfw_scanner.helpers.images")


class _StubImageContext(SimpleNamespace):
    @property
    def accelerated(self) -> bool:
        return bool(getattr(self, "_accelerated", False))


async def _build_image_processing_context(*_args, **_kwargs):
    return _StubImageContext(
        guild_id=None,
        settings_map={},
        allowed_categories=[],
        moderation_threshold=0.7,
        high_accuracy=False,
        limits=SimpleNamespace(is_premium=False),
        _accelerated=False,
    )


images_stub.ImageProcessingContext = _StubImageContext
images_stub.build_image_processing_context = _build_image_processing_context
sys.modules.setdefault("modules.nsfw_scanner.helpers.images", images_stub)

mysql_stub = types.ModuleType("modules.utils.mysql")


async def _mysql_get_settings(*_args, **_kwargs):
    return {}


async def _mysql_get_premium_status(*_args, **_kwargs):
    return {}


async def _mysql_resolve_plan(*_args, **_kwargs):
    return "free"


mysql_stub.get_settings = _mysql_get_settings
mysql_stub.get_premium_status = _mysql_get_premium_status
mysql_stub.resolve_guild_plan = _mysql_resolve_plan
sys.modules.setdefault("modules.utils.mysql", mysql_stub)

clip_vectors_stub = types.ModuleType("modules.utils.clip_vectors")
clip_vectors_stub.is_available = lambda: False
clip_vectors_stub.register_failure_callback = lambda *_args, **_kwargs: None
clip_vectors_stub.query_similar = lambda *_args, **_kwargs: []
sys.modules.setdefault("modules.utils.clip_vectors", clip_vectors_stub)

orchestrator_stub = types.ModuleType("modules.nsfw_scanner.scanner.orchestrator")
orchestrator_stub.NSFWScanner = object
sys.modules.setdefault("modules.nsfw_scanner.scanner.orchestrator", orchestrator_stub)

from modules.nsfw_scanner.scanner.media_collector import (
    collect_media_items,
    hydrate_message,
)


def _build_embed(*, video_url: str | None = None, image_url: str | None = None) -> SimpleNamespace:
    def _wrap(url: str | None) -> SimpleNamespace | None:
        if url is None:
            return None
        return SimpleNamespace(url=url)

    return SimpleNamespace(
        video=_wrap(video_url),
        image=_wrap(image_url),
        thumbnail=None,
    )


class _DummyBot:
    def get_emoji(self, _emoji_id):
        return None


class _WeakNamespace:
    __slots__ = ("__dict__", "__weakref__")

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def test_collect_media_items_deduplicates_tenor_variants():
    embed = _build_embed(
        video_url="https://media1.tenor.com/abcdefghijk/video.mp4",
        image_url="https://tenor.com/abcdefghijk.gif",
    )
    message = SimpleNamespace(
        attachments=[],
        embeds=[embed],
        stickers=[],
        content="",
    )
    context = SimpleNamespace(tenor_allowed=True)

    items = collect_media_items(message, _DummyBot(), context)

    assert len(items) == 1
    assert items[0].tenor is True
    assert items[0].url == "https://media1.tenor.com/abcdefghijk/video.mp4"


def test_collect_media_items_uses_proxy_url_without_fallback():
    attachment = SimpleNamespace(
        proxy_url="https://media.discordapp.net/attachments/1/2/image0.gif?width=120&height=80",
        url=None,
        filename="image0.gif",
        size=1337,
        id=42,
    )
    message = _WeakNamespace(
        attachments=[attachment],
        embeds=[],
        stickers=[],
        message_snapshots=[],
        id=99,
        channel=SimpleNamespace(id=123),
        guild=SimpleNamespace(id=456),
        content="",
    )
    context = SimpleNamespace(tenor_allowed=True)

    items = collect_media_items(message, _DummyBot(), context)

    assert len(items) == 1
    assert items[0].metadata.get("fallback_urls") is None
    assert items[0].url == "https://media.discordapp.net/attachments/1/2/image0.gif?width=120&height=80"


def test_collect_media_items_records_original_url_when_proxy_available():
    proxy_url = "https://media.discordapp.net/attachments/1/2/image0.gif?width=120&height=80"
    original_url = "https://cdn.discordapp.com/attachments/1/2/image0.gif"
    attachment = SimpleNamespace(
        proxy_url=proxy_url,
        url=original_url,
        filename="image0.gif",
        size=1337,
        id=42,
    )
    message = _WeakNamespace(
        attachments=[attachment],
        embeds=[],
        stickers=[],
        message_snapshots=[],
        id=99,
        channel=SimpleNamespace(id=123),
        guild=SimpleNamespace(id=456),
        content="",
    )
    context = SimpleNamespace(tenor_allowed=True)

    items = collect_media_items(message, _DummyBot(), context)

    assert len(items) == 1
    metadata = items[0].metadata
    assert metadata.get("proxy_url") == proxy_url
    assert metadata.get("original_url") == original_url
    assert metadata.get("fallback_urls") == [original_url]
    assert items[0].url == proxy_url


def test_collect_media_items_prefers_signed_content_url():
    signed_url = (
        "https://cdn.discordapp.com/attachments/1/2/image0.gif"
        "?ex=6612b18b&is=65f03c8b&hm=deadbeefcafebabe"
    )
    proxy_url = "https://media.discordapp.net/attachments/1/2/image0.gif"
    original_url = "https://cdn.discordapp.com/attachments/1/2/image0.gif"
    attachment = SimpleNamespace(
        proxy_url=proxy_url,
        url=original_url,
        filename="image0.gif",
        size=1337,
        id=42,
        hash="abcdef",
    )
    message = _WeakNamespace(
        attachments=[attachment],
        embeds=[],
        stickers=[],
        message_snapshots=[],
        id=99,
        channel=SimpleNamespace(id=123),
        guild=SimpleNamespace(id=456),
        content=f"look at this {signed_url}",
    )
    context = SimpleNamespace(tenor_allowed=True)

    hydrated_message = asyncio.run(hydrate_message(message))
    assert hydrated_message is message

    items = collect_media_items(message, _DummyBot(), context)

    assert len(items) == 1
    item = items[0]
    assert item.url == signed_url
    metadata = item.metadata
    assert metadata.get("original_url") == signed_url
    assert metadata.get("proxy_url") == proxy_url
    assert metadata.get("fallback_urls") == [proxy_url, original_url]
    assert metadata.get("signed_content_urls") == [signed_url]
