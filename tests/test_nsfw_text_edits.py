import asyncio
import importlib.util
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

os.environ.setdefault("FERNET_SECRET_KEY", "dGVzdF9zZWNyZXRfa2V5X2Zvcl9tb2Rib3RfZGV2IQ==")

if "discord" not in sys.modules:
    discord_stub = types.ModuleType("discord")

    class _DummyEmbed:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def add_field(self, *args, **kwargs):
            return None

        def set_thumbnail(self, *args, **kwargs):
            return None

    class _DummyColor:
        @staticmethod
        def red():
            return 0xFF0000

        @staticmethod
        def orange():
            return 0xFFA500

    class _DummyAllowedMentions:
        @staticmethod
        def none():
            return None

    class _DummyGuildPermissions:
        def __init__(self, moderate_members: bool = False):
            self.moderate_members = moderate_members

    class _DummyUser:
        def __init__(self):
            self.roles = ()
            self.guild_permissions = _DummyGuildPermissions()
            self.display_avatar = types.SimpleNamespace(url="https://example.com/avatar.png")
            self.mention = "@user"

        async def send(self, *_args, **_kwargs):
            return None

    discord_stub.User = _DummyUser
    discord_stub.TextChannel = type("TextChannel", (), {})
    discord_stub.Member = type("Member", (), {})
    discord_stub.Message = type("Message", (), {})
    discord_stub.Interaction = type("Interaction", (), {"user": _DummyUser()})

    discord_utils_module = types.ModuleType("discord.utils")

    def _dummy_get(_iterable, **_attrs):
        return None

    discord_utils_module.get = _dummy_get

    app_commands_module = types.SimpleNamespace(check=lambda func: func)

    discord_stub.Embed = _DummyEmbed
    discord_stub.Color = _DummyColor
    discord_stub.AllowedMentions = _DummyAllowedMentions
    discord_stub.Forbidden = type("Forbidden", (Exception,), {})
    discord_stub.NotFound = type("NotFound", (Exception,), {})
    discord_stub.HTTPException = type("HTTPException", (Exception,), {})
    discord_stub.utils = discord_utils_module
    discord_stub.app_commands = app_commands_module
    sys.modules["discord"] = discord_stub
    sys.modules["discord.utils"] = discord_utils_module

if "discord.ext" not in sys.modules:
    discord_ext = types.ModuleType("discord.ext")
    commands_module = types.ModuleType("discord.ext.commands")
    tasks_module = types.ModuleType("discord.ext.tasks")

    class _DummyCog:
        @classmethod
        def listener(cls, *_args, **_kwargs):
            def decorator(func):
                return func

            return decorator

    commands_module.Cog = _DummyCog
    class _DummyBot:
        def __init__(self, *args, **kwargs):
            self._cogs = {}

        def add_cog(self, cog):
            self._cogs[getattr(cog, "__class__", type("Anon", (), {})).__name__] = cog

        def get_cog(self, name):
            return self._cogs.get(name)

    commands_module.Bot = _DummyBot
    commands_module.AutoShardedBot = _DummyBot
    commands_module.AutoShardedClient = _DummyBot
    tasks_module.loop = lambda *_args, **_kwargs: (lambda func: func)
    discord_ext.commands = commands_module
    discord_ext.tasks = tasks_module
    sys.modules["discord.ext"] = discord_ext
    sys.modules["discord.ext.commands"] = commands_module
    sys.modules["discord.ext.tasks"] = tasks_module

if "aiohttp" not in sys.modules:
    aiohttp_module = types.ModuleType("aiohttp")

    class _DummyClientSession:
        def __init__(self, *args, **kwargs):
            pass

        async def close(self):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            class _DummyResponse:
                def __init__(self):
                    self.content = types.SimpleNamespace(iter_chunked=lambda _size: iter(()))

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *_args):
                    return None

                def raise_for_status(self):
                    return None

            return _DummyResponse()

    aiohttp_module.ClientSession = _DummyClientSession
    sys.modules["aiohttp"] = aiohttp_module

if "diskcache" not in sys.modules:
    diskcache_module = types.ModuleType("diskcache")

    class _DummyCache:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, *_args, **_kwargs):
            return None

        def set(self, *_args, **_kwargs):
            return None

    diskcache_module.Cache = _DummyCache
    sys.modules["diskcache"] = diskcache_module

if "modules.utils.mysql" not in sys.modules:
    mysql_stub = types.ModuleType("modules.utils.mysql")

    async def _default_async(*_args, **_kwargs):
        return None

    mysql_stub.get_settings = _default_async
    mysql_stub.is_accelerated = _default_async
    mysql_stub.execute_query = _default_async
    sys.modules["modules.utils.mysql"] = mysql_stub
    utils_parent = sys.modules.get("modules.utils")
    if utils_parent is None:
        utils_parent = types.ModuleType("modules.utils")
        utils_parent.__path__ = [str(PROJECT_ROOT / "modules" / "utils")]
        sys.modules["modules.utils"] = utils_parent
    setattr(utils_parent, "mysql", mysql_stub)
    modules_parent = sys.modules.get("modules")
    if modules_parent is None:
        modules_parent = types.ModuleType("modules")
        modules_parent.__path__ = [str(PROJECT_ROOT / "modules")]
        sys.modules["modules"] = modules_parent
    setattr(modules_parent, "utils", utils_parent)

if "modules.worker_queue" not in sys.modules:
    worker_queue_path = PROJECT_ROOT / "modules" / "worker_queue.py"
    worker_queue_spec = importlib.util.spec_from_file_location(
        "modules.worker_queue",
        worker_queue_path,
    )
    assert worker_queue_spec is not None and worker_queue_spec.loader is not None
    worker_queue_module = importlib.util.module_from_spec(worker_queue_spec)
    sys.modules["modules.worker_queue"] = worker_queue_module
    worker_queue_spec.loader.exec_module(worker_queue_module)

if "modules.worker_queue_alerts" not in sys.modules:
    worker_queue_alerts_stub = types.ModuleType("modules.worker_queue_alerts")

    class _DummySingularTaskReporter:
        def __init__(self, *args, **kwargs):
            pass

    worker_queue_alerts_stub.SingularTaskReporter = _DummySingularTaskReporter
    sys.modules["modules.worker_queue_alerts"] = worker_queue_alerts_stub


def test_handle_message_edit_triggers_text_scan(monkeypatch):
    handlers_spec = importlib.util.spec_from_file_location(
        "test_handlers_module",
        PROJECT_ROOT / "cogs" / "aggregated_moderation" / "handlers.py",
    )
    handlers_module = importlib.util.module_from_spec(handlers_spec)
    assert handlers_spec.loader is not None
    handlers_spec.loader.exec_module(handlers_module)
    ModerationHandlers = handlers_module.ModerationHandlers
    from modules.nsfw_scanner.settings_keys import (
        NSFW_TEXT_ENABLED_SETTING,
        NSFW_TEXT_EXCLUDED_CHANNELS_SETTING,
    )

    settings = {
        "nsfw-enabled": False,
        NSFW_TEXT_ENABLED_SETTING: True,
        NSFW_TEXT_EXCLUDED_CHANNELS_SETTING: [],
        "scan-age-restricted": False,
        "exclude-channels": [],
        "nsfw-channel-notify": None,
    }

    async def fake_get_settings(guild_id, key, *_args, **_kwargs):
        return settings.get(key)

    monkeypatch.setattr("modules.utils.mysql.get_settings", fake_get_settings)

    nsfw_calls = {}

    async def fake_is_nsfw(**kwargs):
        nsfw_calls.update(kwargs)
        return {"flagged": True, "text_flagged": True, "media_flagged": False}

    scanner = SimpleNamespace(is_nsfw=fake_is_nsfw)

    async def fake_enqueue(coro, guild_id, **kwargs):
        nsfw_calls["enqueued_guild"] = guild_id
        nsfw_calls["task_kind"] = kwargs.get("task_kind")
        await coro

    dummy_bot = SimpleNamespace(translate=lambda *args, **kwargs: {"title": "", "description": ""})

    handlers_module.handle_nsfw_content = AsyncMock()

    handlers = ModerationHandlers(bot=dummy_bot, scanner=scanner, enqueue_task=fake_enqueue)

    author = SimpleNamespace(
        bot=False,
        id=123,
        mention="<@123>",
        display_avatar=SimpleNamespace(url="https://example.com/avatar.png"),
    )
    channel = SimpleNamespace(id=456, parent=None, is_nsfw=lambda: False)
    guild = SimpleNamespace(id=789)

    after_message = SimpleNamespace(
        author=author,
        channel=channel,
        guild=guild,
        content="edited content",
        attachments=[],
        embeds=[],
        stickers=[],
        reactions=[],
    )

    cached_before = SimpleNamespace(content="original content")

    async def run_test():
        await handlers.handle_message_edit(cached_before, after_message)

        assert nsfw_calls.get("scan_text") is True
        assert nsfw_calls.get("scan_media") is False
        assert nsfw_calls.get("nsfw_callback") is handlers_module.handle_nsfw_content
        assert nsfw_calls.get("message") is after_message
        assert nsfw_calls.get("guild_id") == guild.id

    asyncio.run(run_test())


def test_event_dispatcher_calls_aggregated_moderation_on_edit(monkeypatch):
    dispatcher_spec = importlib.util.spec_from_file_location(
        "test_event_dispatcher_module",
        PROJECT_ROOT / "cogs" / "event_dispatcher.py",
    )
    dispatcher_module = importlib.util.module_from_spec(dispatcher_spec)
    assert dispatcher_spec.loader is not None
    dispatcher_spec.loader.exec_module(dispatcher_module)
    EventDispatcherCog = dispatcher_module.EventDispatcherCog

    cached_message = SimpleNamespace(content="before")

    async def fake_get_cached_message(_guild_id, _message_id):
        return cached_message

    monkeypatch.setattr(dispatcher_module, "get_cached_message", fake_get_cached_message)

    agg_cog = SimpleNamespace(handle_message_edit=AsyncMock())
    banned_cog = SimpleNamespace(handle_message_edit=AsyncMock())
    monitoring_cog = SimpleNamespace(handle_message_edit=AsyncMock())
    auto_cog = SimpleNamespace(handle_message_edit=AsyncMock())

    class _DummyBot(SimpleNamespace):
        def get_cog(self, name):
            return getattr(self, name)

    bot = _DummyBot(
        AggregatedModerationCog=agg_cog,
        BannedWordsCog=banned_cog,
        MonitoringCog=monitoring_cog,
        AutonomousModeratorCog=auto_cog,
    )

    dispatcher = EventDispatcherCog.__new__(EventDispatcherCog)
    dispatcher.bot = bot

    author = SimpleNamespace(bot=False)
    after = SimpleNamespace(content="after", author=author)
    payload = SimpleNamespace(guild_id=1, message_id=2, channel_id=3, message=after)

    async def run_test():
        await dispatcher.on_raw_message_edit(payload)
        assert agg_cog.handle_message_edit.await_count == 1
        call_args = agg_cog.handle_message_edit.await_args
        assert call_args.args == (cached_message, after)
        assert call_args.kwargs == {}

    asyncio.run(run_test())
