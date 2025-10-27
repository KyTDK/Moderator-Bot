import sys
import types
from pathlib import Path
import asyncio
from types import SimpleNamespace

project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.append(str(project_root))

import importlib.machinery


class _DummyColor:
    def __init__(self, value: int = 0):
        self.value = value

    @classmethod
    def red(cls):
        return cls(0xFF0000)


class _DummyEmbed:
    def __init__(self, *args, **kwargs):
        self.fields: list[SimpleNamespace] = []

    def add_field(self, *, name, value, inline=False):
        self.fields.append(SimpleNamespace(name=name, value=value, inline=inline))

    def set_footer(self, *args, **kwargs):
        pass

    def set_thumbnail(self, *args, **kwargs):
        pass


discord_stub = sys.modules.get("discord")
if discord_stub is None:
    discord_stub = types.ModuleType("discord")
    sys.modules["discord"] = discord_stub

if not getattr(discord_stub, "__path__", None):
    discord_stub.__path__ = []
discord_stub.__package__ = "discord"
spec = getattr(discord_stub, "__spec__", None)
if spec is None or not getattr(spec, "submodule_search_locations", None):
    spec = importlib.machinery.ModuleSpec("discord", loader=None, is_package=True)
    spec.submodule_search_locations = []
    discord_stub.__spec__ = spec

discord_stub.Color = _DummyColor
discord_stub.Embed = _DummyEmbed
discord_stub.Forbidden = getattr(discord_stub, "Forbidden", type("Forbidden", (Exception,), {}))
discord_stub.HTTPException = getattr(
    discord_stub, "HTTPException", type("HTTPException", (Exception,), {})
)

errors_stub = sys.modules.get("discord.errors")
if errors_stub is None:
    errors_stub = types.ModuleType("discord.errors")
    sys.modules["discord.errors"] = errors_stub
errors_stub.NotFound = getattr(errors_stub, "NotFound", type("NotFound", (Exception,), {}))
errors_stub.Forbidden = getattr(errors_stub, "Forbidden", discord_stub.Forbidden)
errors_stub.HTTPException = getattr(errors_stub, "HTTPException", discord_stub.HTTPException)
discord_stub.errors = errors_stub

ext_stub = sys.modules.get("discord.ext")
if ext_stub is None:
    ext_stub = types.ModuleType("discord.ext")
    sys.modules["discord.ext"] = ext_stub

commands_stub = sys.modules.get("discord.ext.commands")
if commands_stub is None:
    commands_stub = types.ModuleType("discord.ext.commands")
    sys.modules["discord.ext.commands"] = commands_stub

class _DummyCog:
    @staticmethod
    def listener(*_args, **_kwargs):
        def decorator(func):
            return func

        return decorator


class _DummyBot:
    pass


commands_stub.Cog = getattr(commands_stub, "Cog", _DummyCog)
commands_stub.Bot = getattr(commands_stub, "Bot", _DummyBot)
ext_stub.commands = commands_stub
discord_stub.ext = ext_stub

utils_stub = sys.modules.get("discord.utils")
if utils_stub is None:
    utils_stub = types.ModuleType("discord.utils")
    sys.modules["discord.utils"] = utils_stub
utils_stub.__package__ = "discord"
utils_spec = getattr(utils_stub, "__spec__", None)
if utils_spec is None:
    utils_spec = importlib.machinery.ModuleSpec("discord.utils", loader=None, is_package=False)
    utils_stub.__spec__ = utils_spec

def _utcnow():
    return None

utils_stub.utcnow = _utcnow
discord_stub.utils = utils_stub

media_worker_pkg = sys.modules.get("modules.nsfw_scanner.scanner.media_worker")
if media_worker_pkg is None:
    media_worker_pkg = types.ModuleType("modules.nsfw_scanner.scanner.media_worker")
    media_worker_pkg.__path__ = []
    media_worker_pkg.__package__ = "modules.nsfw_scanner.scanner.media_worker"
    sys.modules["modules.nsfw_scanner.scanner.media_worker"] = media_worker_pkg

cache_path = (
    project_root
    / "modules"
    / "nsfw_scanner"
    / "scanner"
    / "media_worker"
    / "cache.py"
)
cache_spec = importlib.util.spec_from_file_location(
    "modules.nsfw_scanner.scanner.media_worker.cache",
    cache_path,
)
cache_module = importlib.util.module_from_spec(cache_spec)
assert cache_spec.loader is not None
cache_spec.loader.exec_module(cache_module)
sys.modules[cache_spec.name] = cache_module
media_worker_pkg.cache = cache_module

import importlib.util

import pytest

diagnostics_path = (
    project_root
    / "modules"
    / "nsfw_scanner"
    / "scanner"
    / "media_worker"
    / "diagnostics.py"
)
diagnostics_spec = importlib.util.spec_from_file_location(
    "modules.nsfw_scanner.scanner.media_worker.diagnostics",
    diagnostics_path,
)
diagnostics = importlib.util.module_from_spec(diagnostics_spec)
assert diagnostics_spec.loader is not None
diagnostics_spec.loader.exec_module(diagnostics)
sys.modules[diagnostics_spec.name] = diagnostics

from modules.nsfw_scanner.scanner.work_item import MediaWorkItem


def test_notify_download_failure_labels_urls(monkeypatch):
    captured: dict[str, object] = {}

    async def _fake_send_log_message(_bot, *, embed, logger, context):
        captured["embed"] = embed
        captured["context"] = context
        return True

    monkeypatch.setattr(diagnostics, "send_log_message", _fake_send_log_message)
    monkeypatch.setattr(diagnostics, "LOG_CHANNEL_ID", 123)

    proxy_url = "https://media.discordapp.net/attachments/1/2/image0.gif?width=120&height=80"
    original_url = "https://cdn.discordapp.com/attachments/1/2/image0.gif"
    other_url = "https://example.com/fallback.gif"

    item = MediaWorkItem(
        source="attachment",
        label="image0.gif",
        url=proxy_url,
        metadata={
            "proxy_url": proxy_url,
            "original_url": original_url,
        },
    )

    error = SimpleNamespace(
        status=404,
        message="Not found",
        headers={},
        request_info=None,
    )

    scanner = SimpleNamespace(bot=object())
    context = SimpleNamespace(guild_id=1)

    async def _run() -> None:
        await diagnostics.notify_download_failure(
            scanner,
            item=item,
            context=context,
            message=None,
            attempted_urls=[proxy_url, original_url, other_url],
            fallback_urls=[other_url],
            refreshed_urls=None,
            error=error,
            logger=None,
        )

    asyncio.run(_run())

    embed = captured.get("embed")
    assert embed is not None
    field_names = [field.name for field in embed.fields]
    assert "Proxy URL" in field_names
    assert "Original URL" in field_names
    assert "Additional Attempted URLs" in field_names

    proxy_field = next(field for field in embed.fields if field.name == "Proxy URL")
    original_field = next(field for field in embed.fields if field.name == "Original URL")
    additional_field = next(
        field for field in embed.fields if field.name == "Additional Attempted URLs"
    )

    assert proxy_url in proxy_field.value
    assert original_url in original_field.value
    assert other_url in additional_field.value
