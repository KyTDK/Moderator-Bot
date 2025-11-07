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
_prepare_request_url = downloads._prepare_request_url
_resolve_stream_config = downloads._resolve_stream_config
DEFAULT_BUFFER_SIZE = downloads.DEFAULT_BUFFER_SIZE
DEFAULT_CHUNK_SIZE = downloads.DEFAULT_CHUNK_SIZE
MAX_BUFFER_SIZE = downloads.MAX_BUFFER_SIZE
MAX_CHUNK_SIZE = downloads.MAX_CHUNK_SIZE
MIN_CHUNK_SIZE = downloads.MIN_CHUNK_SIZE
URL = downloads.URL


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


def test_prepare_request_url_preserves_percent_encoding():
    encoded_url = (
        "https://distrokid.imgix.net/http%3A%2F%2Fgather.fandalism.com%2Fmock.jpg"
        "?mark=http%3A%2F%2Fgather.fandalism.com%2Foverlay.png"
    )
    prepared = _prepare_request_url(encoded_url)
    assert isinstance(prepared, URL)
    assert str(prepared) == encoded_url

    plain_url = "https://example.com/path"
    assert _prepare_request_url(plain_url) == plain_url


async def _start_test_site(*routes):
    app = web.Application()
    for method, path, handler in routes:
        app.router.add_route(method, path, handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    return runner, site, base_url


@pytest.mark.anyio
async def test_temp_download_falls_back_for_discord_static_emoji():
    gif_path = "/emojis/1425034030985908264.gif"
    webp_path = "/emojis/1425034030985908264.webp"
    png_path = "/emojis/1425034030985908264.png"
    payload = b"\x89PNG\r\n\x1a\n" + (b"\x00" * 1024)
    downloads._DISCORD_EMOJI_HOSTS.add("127.0.0.1")

    async def gif_handler(request):
        return web.json_response({"message": "Invalid resource"}, status=415)

    async def webp_handler(request):
        return web.json_response({"message": "not found"}, status=404)

    async def png_handler(request):
        return web.Response(body=payload, content_type="image/png")

    runner, site, base_url = await _start_test_site(
        ("GET", gif_path, gif_handler),
        ("HEAD", gif_path, gif_handler),
        ("GET", webp_path, webp_handler),
        ("HEAD", webp_path, webp_handler),
        ("GET", png_path, png_handler),
        ("HEAD", png_path, png_handler),
    )

    try:
        gif_url = f"{base_url}{gif_path}"

        async with aiohttp.ClientSession() as session:
            async with temp_download(session, gif_url) as result:
                assert result.path.endswith(".png")
                assert result.telemetry.resolved_url.endswith(".png")
                with open(result.path, "rb") as handle:
                    assert handle.read() == payload
    finally:
        downloads._DISCORD_EMOJI_HOSTS.discard("127.0.0.1")
        await runner.cleanup()


@pytest.mark.anyio
async def test_temp_download_prefers_tenor_video_variant(monkeypatch):
    downloads._DISCORD_EMOJI_HOSTS.add("127.0.0.1")
    original_is_tenor = downloads.is_tenor_host
    monkeypatch.setattr(
        downloads,
        "is_tenor_host",
        lambda host: host.split(":")[0] == "127.0.0.1" or original_is_tenor(host),
    )

    gif_path = "/media/animated.gif"
    mp4_path = "/media/animated.mp4"
    payload = b"mp4data" * 1024

    async def gif_handler(request):
        return web.Response(body=b"gifdata", content_type="image/gif")

    async def mp4_handler(request):
        return web.Response(body=payload, content_type="video/mp4")

    runner, _, base_url = await _start_test_site(
        ("HEAD", gif_path, gif_handler),
        ("GET", gif_path, gif_handler),
        ("HEAD", mp4_path, mp4_handler),
        ("GET", mp4_path, mp4_handler),
    )

    try:
        url = f"{base_url}{gif_path}"
        async with aiohttp.ClientSession() as session:
            async with temp_download(session, url) as result:
                assert result.path.endswith(".mp4")
                assert result.telemetry.resolved_url.endswith(".mp4")
                with open(result.path, "rb") as handle:
                    assert handle.read() == payload
    finally:
        downloads._DISCORD_EMOJI_HOSTS.discard("127.0.0.1")
        await runner.cleanup()


@pytest.mark.anyio
async def test_temp_download_falls_back_when_tenor_video_unavailable(monkeypatch):
    downloads._DISCORD_EMOJI_HOSTS.add("127.0.0.1")
    original_is_tenor = downloads.is_tenor_host
    monkeypatch.setattr(
        downloads,
        "is_tenor_host",
        lambda host: host.split(":")[0] == "127.0.0.1" or original_is_tenor(host),
    )

    gif_path = "/media/static.gif"
    mp4_path = "/media/static.mp4"
    payload = b"gif-bytes"

    async def gif_handler(request):
        return web.Response(body=payload, content_type="image/gif")

    async def mp4_handler(request):
        return web.Response(status=404)

    runner, _, base_url = await _start_test_site(
        ("HEAD", gif_path, gif_handler),
        ("GET", gif_path, gif_handler),
        ("HEAD", mp4_path, mp4_handler),
        ("GET", mp4_path, mp4_handler),
    )

    try:
        url = f"{base_url}{gif_path}"
        async with aiohttp.ClientSession() as session:
            async with temp_download(session, url) as result:
                assert result.path.endswith(".gif")
                assert result.telemetry.resolved_url.endswith(".gif")
                with open(result.path, "rb") as handle:
                    assert handle.read() == payload
    finally:
        downloads._DISCORD_EMOJI_HOSTS.discard("127.0.0.1")
        await runner.cleanup()


@pytest.mark.anyio
async def test_temp_download_respects_custom_extension():
    path = "/file.bin"
    payload = b"hello world"

    async def handler(request):
        return web.Response(body=payload, content_type="application/octet-stream")

    runner, _, base_url = await _start_test_site(
        ("HEAD", path, handler),
        ("GET", path, handler),
    )

    try:
        url = f"{base_url}{path}"
        async with aiohttp.ClientSession() as session:
            async with temp_download(session, url, ext="dat") as result:
                assert result.path.endswith(".dat")
                with open(result.path, "rb") as handle:
                    assert handle.read() == payload
    finally:
        await runner.cleanup()


@pytest.mark.anyio
async def test_temp_download_ignores_head_failure(monkeypatch):
    path = "/payload"
    payload = b"data"

    async def get_handler(request):
        return web.Response(body=payload, content_type="application/octet-stream")

    runner, _, base_url = await _start_test_site(
        ("GET", path, get_handler),
    )

    async def fake_probe(session, url):
        return False, None, None

    monkeypatch.setattr(downloads, "_probe_head", fake_probe)

    try:
        url = f"{base_url}{path}"
        async with aiohttp.ClientSession() as session:
            async with temp_download(session, url) as result:
                assert result.telemetry.content_length is None
                with open(result.path, "rb") as handle:
                    assert handle.read() == payload
    finally:
        await runner.cleanup()


@pytest.mark.anyio
async def test_temp_download_cap_enforced():
    path = "/large"
    payload = b"A" * 8192

    async def handler(request):
        return web.Response(body=payload, content_type="application/octet-stream")

    runner, _, base_url = await _start_test_site(
        ("HEAD", path, handler),
        ("GET", path, handler),
    )

    try:
        url = f"{base_url}{path}"
        async with aiohttp.ClientSession() as session:
            with pytest.raises(ValueError) as excinfo:
                async with temp_download(session, url, download_cap_bytes=1024):
                    pass
            assert "Download exceeds cap" in str(excinfo.value)
    finally:
        await runner.cleanup()


@pytest.mark.anyio
async def test_temp_download_falls_back_after_cap_skip(monkeypatch):
    downloads._DISCORD_EMOJI_HOSTS.add("127.0.0.1")
    original_is_tenor = downloads.is_tenor_host
    monkeypatch.setattr(
        downloads,
        "is_tenor_host",
        lambda host: host == "127.0.0.1" or original_is_tenor(host),
    )

    gif_path = "/media/emoji.gif"
    mp4_path = "/media/emoji.mp4"
    gif_payload = b"gif"
    mp4_payload = b"mp4"

    async def gif_handler(request):
        return web.Response(body=gif_payload, content_type="image/gif")

    async def mp4_handler(request):
        response = web.Response(body=mp4_payload, content_type="video/mp4")
        response.headers["Content-Length"] = str(10_000)
        return response

    async def mp4_head(request):
        response = web.Response(status=200)
        response.headers["Content-Length"] = str(10_000)
        return response

    runner, _, base_url = await _start_test_site(
        ("HEAD", gif_path, gif_handler),
        ("GET", gif_path, gif_handler),
        ("HEAD", mp4_path, mp4_head),
        ("GET", mp4_path, mp4_handler),
    )

    try:
        url = f"{base_url}{gif_path}"
        async with aiohttp.ClientSession() as session:
            async with temp_download(session, url, download_cap_bytes=2048) as result:
                assert result.path.endswith(".gif")
                assert result.telemetry.resolved_url.endswith(".gif")
                with open(result.path, "rb") as handle:
                    assert handle.read() == gif_payload
    finally:
        downloads._DISCORD_EMOJI_HOSTS.discard("127.0.0.1")
        await runner.cleanup()


@pytest.mark.anyio
async def test_temp_download_stream_probe_limit():
    path = "/stream"
    chunk = b"x" * 65536
    total_chunks = (downloads.PROBE_LIMIT_BYTES // len(chunk)) + 2

    async def head_handler(request):
        return web.Response(status=200)

    async def stream_handler(request):
        resp = web.StreamResponse(status=200)
        resp.content_type = "application/octet-stream"
        await resp.prepare(request)
        for _ in range(total_chunks):
            await resp.write(chunk)
        await resp.write_eof()
        return resp

    runner, _, base_url = await _start_test_site(
        ("HEAD", path, head_handler),
        ("GET", path, stream_handler),
    )

    try:
        url = f"{base_url}{path}"
        async with aiohttp.ClientSession() as session:
            with pytest.raises(ValueError) as excinfo:
                async with temp_download(session, url):
                    pass
            assert "probe window" in str(excinfo.value)
    finally:
        await runner.cleanup()


@pytest.mark.anyio
async def test_temp_download_raises_when_all_candidates_fail():
    downloads._DISCORD_EMOJI_HOSTS.add("127.0.0.1")
    gif_path = "/emojis/fail.gif"
    png_path = "/emojis/fail.png"

    async def fail_415(request):
        return web.Response(status=415)

    async def fail_404(request):
        return web.Response(status=404)

    runner, _, base_url = await _start_test_site(
        ("HEAD", gif_path, fail_415),
        ("GET", gif_path, fail_415),
        ("HEAD", png_path, fail_404),
        ("GET", png_path, fail_404),
    )

    try:
        url = f"{base_url}{gif_path}"
        async with aiohttp.ClientSession() as session:
            with pytest.raises(aiohttp.ClientResponseError) as excinfo:
                async with temp_download(session, url):
                    pass
            assert excinfo.value.status in {404, 415}
    finally:
        downloads._DISCORD_EMOJI_HOSTS.discard("127.0.0.1")
        await runner.cleanup()


@pytest.mark.anyio
async def test_temp_download_requires_session():
    with pytest.raises(RuntimeError):
        async with temp_download(None, "http://example.com"):
            pass
