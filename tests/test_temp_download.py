import os
from pathlib import Path
import sys

import importlib.util
import pytest

aiohttp = pytest.importorskip("aiohttp")
from aiohttp import web

# Ensure project root is on the import path
project_root = Path(__file__).resolve().parents[1]
sys.path.append(str(project_root))

# Provide lightweight stubs for discord to satisfy package imports during tests
import types

if "discord" not in sys.modules:
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
    tasks_stub = types.ModuleType("discord.ext.tasks")

    def _loop(*_args, **_kwargs):
        def _decorator(func):
            return func

        return _decorator

    tasks_stub.loop = _loop
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
    commands_stub.AutoShardedBot = _DummyBot

    ext_stub.commands = commands_stub
    ext_stub.tasks = tasks_stub
    discord_stub.errors = errors_stub
    discord_stub.ext = ext_stub

    sys.modules["discord"] = discord_stub
    sys.modules["discord.errors"] = errors_stub
    sys.modules["discord.ext"] = ext_stub
    sys.modules["discord.ext.commands"] = commands_stub
    sys.modules["discord.ext.tasks"] = tasks_stub

if "apnggif" not in sys.modules:
    apnggif_stub = types.ModuleType("apnggif")

    def _noop_apnggif(*args, **kwargs):
        return None

    apnggif_stub.apnggif = _noop_apnggif
    sys.modules["apnggif"] = apnggif_stub

if "pillow_avif" not in sys.modules:
    sys.modules["pillow_avif"] = types.ModuleType("pillow_avif")

downloads_path = project_root / "modules" / "nsfw_scanner" / "helpers" / "downloads.py"
spec = importlib.util.spec_from_file_location("modules.nsfw_scanner.helpers.downloads", downloads_path)
downloads = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(downloads)

temp_download = downloads.temp_download
_resolve_stream_config = downloads._resolve_stream_config
DEFAULT_BUFFER_SIZE = downloads.DEFAULT_BUFFER_SIZE
DEFAULT_CHUNK_SIZE = downloads.DEFAULT_CHUNK_SIZE
MAX_BUFFER_SIZE = downloads.MAX_BUFFER_SIZE
MAX_CHUNK_SIZE = downloads.MAX_CHUNK_SIZE
MIN_CHUNK_SIZE = downloads.MIN_CHUNK_SIZE


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_temp_download_writes_to_disk_and_cleans_up():
    payload = b"0123456789" * 131072  # ~1.25 MiB to exercise flushing

    async def handler(request):
        return web.Response(body=payload)

    app = web.Application()
    app.router.add_get("/", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    try:
        port = site._server.sockets[0].getsockname()[1]
        url = f"http://127.0.0.1:{port}/"

        async with aiohttp.ClientSession() as session:
            captured_path = None
            async with temp_download(session, url, ext="dat") as path:
                captured_path = path
                assert captured_path.endswith(".dat")
                assert os.path.exists(captured_path)
                with open(captured_path, "rb") as handle:
                    assert handle.read() == payload

            assert captured_path is not None
            assert not os.path.exists(captured_path)
    finally:
        await runner.cleanup()


def test_resolve_stream_config_defaults_and_bounds():
    chunk_size, buffer_size = _resolve_stream_config(None)
    assert chunk_size == DEFAULT_CHUNK_SIZE
    assert buffer_size == DEFAULT_BUFFER_SIZE

    small_chunk, small_buffer = _resolve_stream_config(1024)
    assert small_chunk >= MIN_CHUNK_SIZE
    assert small_buffer >= small_chunk

    large_chunk, large_buffer = _resolve_stream_config(64 << 20)  # 64 MiB
    assert MIN_CHUNK_SIZE <= large_chunk <= MAX_CHUNK_SIZE
    assert MIN_CHUNK_SIZE <= small_chunk <= MAX_CHUNK_SIZE
    assert large_chunk <= large_buffer <= MAX_BUFFER_SIZE
