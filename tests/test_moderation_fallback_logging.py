import asyncio
import base64
import enum
import os
import sys
import types
from pathlib import Path

import importlib


project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.append(str(project_root))

os.environ.setdefault(
    "FERNET_SECRET_KEY",
    base64.urlsafe_b64encode(b"0" * 32).decode(),
)

try:
    import discord  # type: ignore
    from discord import app_commands as app_commands_module  # type: ignore

    _ALLOWED_MENTIONS_TYPE = discord.AllowedMentions
except Exception:  # pragma: no cover - fallback when discord.py is unavailable
    discord_stub = types.ModuleType("discord")

    class _DummyEmbed:
        def __init__(self, *_, **kwargs):
            self.title = kwargs.get("title")
            self.description = kwargs.get("description")
            self.color = kwargs.get("color")
            self.fields: list[dict[str, object]] = []
            self.footer = None

        def add_field(self, *, name, value, inline=True):
            self.fields.append({"name": name, "value": value, "inline": inline})

        def set_footer(self, *, text=None):
            self.footer = text

        def set_thumbnail(self, *_, **__):
            return None

        def set_image(self, *_, **__):
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

    class _DummyAllowedMentions:
        @staticmethod
        def none():
            return _DummyAllowedMentions()

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

    class _DummyIntents:
        def __init__(self):
            self.members = False
            self.presences = False
            self.message_content = False
            self.voice_states = False

        @classmethod
        def default(cls):
            return cls()

    class _DummyMemberCacheFlags:
        def __init__(self):
            self.voice = False

        @classmethod
        def none(cls):
            return cls()

    class _DummyView:
        def __init__(self):
            self.items = []

        def add_item(self, item):
            self.items.append(item)

    class _DummyButton:
        def __init__(self, *_, **kwargs):
            self.label = kwargs.get("label")
            self.url = kwargs.get("url")
            self.emoji = kwargs.get("emoji")

    discord_stub.Locale = _DummyLocale
    discord_stub.Embed = _DummyEmbed
    discord_stub.Color = _DummyColor
    discord_stub.AllowedMentions = _DummyAllowedMentions
    discord_stub.Client = type("Client", (), {})
    discord_stub.File = type("File", (), {})
    discord_stub.Forbidden = type("Forbidden", (Exception,), {})
    discord_stub.HTTPException = type("HTTPException", (Exception,), {})
    discord_stub.NotFound = type("NotFound", (Exception,), {})
    discord_stub.Interaction = type("Interaction", (), {})
    discord_stub.User = type("User", (), {})
    discord_stub.TextChannel = type("TextChannel", (), {})
    discord_stub.Message = type("Message", (), {})
    discord_stub.Guild = type("Guild", (), {})
    discord_stub.Member = type("Member", (), {})
    discord_stub.Role = type("Role", (), {})
    discord_stub.Intents = _DummyIntents
    discord_stub.MemberCacheFlags = _DummyMemberCacheFlags
    discord_stub.utils = types.SimpleNamespace(get=lambda *_args, **_kwargs: None)
    discord_stub.abc = types.SimpleNamespace(Messageable=object)
    discord_stub.ui = types.SimpleNamespace(View=_DummyView, Button=_DummyButton)

    sys.modules["discord"] = discord_stub
    discord = discord_stub

    discord_ext_stub = types.ModuleType("discord.ext")
    commands_stub = types.ModuleType("discord.ext.commands")
    tasks_stub = types.ModuleType("discord.ext.tasks")
    app_commands_module = types.ModuleType("discord.app_commands")

    def _loop(*_args, **_kwargs):
        def _decorator(func):
            return func

        return _decorator

    tasks_stub.loop = _loop

    async def _async_noop(*_args, **_kwargs):
        return None

    class _DummyBot:
        def __init__(self, *_, **__):
            self.loop = asyncio.new_event_loop()
            self._connection = types.SimpleNamespace(shard_id=None, shard_count=None)
            self.guilds = []
            self.tree = types.SimpleNamespace(
                translator=None,
                set_translator=_async_noop,
                sync=_async_noop,
            )
            self._closed = False

        def is_closed(self):
            return self._closed

        async def wait_until_ready(self):
            return None

        async def load_extension(self, *_args, **_kwargs):
            return None

    class _DummyCog:
        def __init__(self, *_, **__):
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
    sys.modules["discord.app_commands"] = app_commands_module

    class _Translator:
        async def load(self) -> None:  # pragma: no cover - compatibility shim
            return None

        async def translate(self, *_args, **_kwargs):  # pragma: no cover - compatibility shim
            raise NotImplementedError

    class _TranslationContextLocation(enum.Enum):
        command_name = enum.auto()
        command_description = enum.auto()
        group_name = enum.auto()
        group_description = enum.auto()
        parameter_name = enum.auto()
        parameter_description = enum.auto()
        choice_name = enum.auto()

    class _TranslationContext:
        def __init__(self, *, location, data=None):
            self.location = location
            self.data = data

    class _LocaleStr(str):
        __slots__ = ("extras", "message")

        def __new__(cls, value: str, **extras):
            instance = str.__new__(cls, value)
            instance.extras = extras
            instance.message = value
            return instance

    def _locale_str(value: str, /, **extras):
        return _LocaleStr(value, **extras)

    app_commands_module.Translator = _Translator
    app_commands_module.TranslationContextLocation = _TranslationContextLocation
    app_commands_module.TranslationContext = _TranslationContext
    app_commands_module.locale_str = _locale_str
    discord_stub.app_commands = app_commands_module

    _ALLOWED_MENTIONS_TYPE = _DummyAllowedMentions

importlib.import_module("modules")
utils_pkg = importlib.import_module("modules.utils")

log_channel_stub = types.ModuleType("modules.utils.log_channel")

try:
    importlib.import_module("modules.utils.mysql")
except Exception:
    mysql_stub = types.ModuleType("modules.utils.mysql")
    mysql_stub.get_premium_status = lambda *_args, **_kwargs: None
    mysql_stub.set_premium_status = lambda *_args, **_kwargs: None
    mysql_stub.MYSQL_CONFIG = {}
    mysql_stub.fernet = None
    async def _async_noop(*_args, **_kwargs):
        return None
    mysql_stub.get_settings = _async_noop  # pragma: no cover - minimal shim
    mysql_stub.get_guild_locale = _async_noop  # pragma: no cover - minimal shim
    mysql_stub.get_all_guild_locales = _async_noop  # pragma: no cover - minimal shim
    mysql_stub.add_settings_listener = lambda *_args, **_kwargs: None
    mysql_stub.remove_settings_listener = lambda *_args, **_kwargs: None
    class _ShardAssignment:
        def __init__(self, shard_id=0, shard_count=1):
            self.shard_id = shard_id
            self.shard_count = shard_count
    mysql_stub.ShardAssignment = _ShardAssignment
    sys.modules["modules.utils.mysql"] = mysql_stub
    setattr(utils_pkg, "mysql", mysql_stub)

mod_logging_stub = types.ModuleType("modules.utils.mod_logging")
mod_logging_stub.log_to_channel = lambda *_args, **_kwargs: None
sys.modules["modules.utils.mod_logging"] = mod_logging_stub
setattr(utils_pkg, "mod_logging", mod_logging_stub)


async def _send_log_message(*_args, **_kwargs):
    return True


def _resolve_log_channel(*_args, **_kwargs):
    return None


def _log_serious_issue(*_args, **_kwargs):
    return False


log_channel_stub.send_log_message = _send_log_message
log_channel_stub.resolve_log_channel = _resolve_log_channel
log_channel_stub.log_serious_issue = _log_serious_issue
sys.modules.setdefault("modules.utils.log_channel", log_channel_stub)
setattr(utils_pkg, "log_channel", log_channel_stub)

moderation = importlib.import_module("modules.nsfw_scanner.helpers.moderation")
moderation_state = importlib.import_module("modules.nsfw_scanner.helpers.moderation_state")


def test_report_moderation_fallback_to_log(monkeypatch):
    captured: dict[str, object] = {"calls": 0}

    async def _fake_send_log_message(*args, **kwargs):
        captured["calls"] = captured.get("calls", 0) + 1
        captured["args"] = args
        captured["kwargs"] = kwargs
        return True

    monkeypatch.setattr(moderation, "send_log_message", _fake_send_log_message)

    scanner = types.SimpleNamespace(bot=object())
    metadata = {
        "guild_id": 123,
        "channel_id": 456,
        "message_id": 789,
        "message_jump_url": "https://example.com/message",
        "source_url": "https://cdn.example.com/image.png",
        "moderation_payload_strategy": "remote_url",
    }

    state = moderation_state.ImageModerationState(
        payload_bytes=b"bytes",
        payload_mime="image/jpeg",
        source_url="https://cdn.example.com/image.png",
        use_remote=True,
    )
    state.mark_fallback("remote_retry")

    fallback_notice = state.fallback_message()
    assert fallback_notice

    asyncio.run(
        moderation._report_moderation_fallback_to_log(
            scanner,
            fallback_notice=fallback_notice,
            image_state=state,
            payload_metadata=metadata,
        )
    )

    assert captured["calls"] == 1
    kwargs = captured["kwargs"]
    assert kwargs["embed"].title == "Moderator API fallback triggered"
    assert kwargs["embed"].description == fallback_notice
    assert kwargs["context"] == "nsfw_scanner.moderation_fallback"
    assert kwargs["allowed_mentions"].__class__ is _ALLOWED_MENTIONS_TYPE
    assert metadata.get("fallback_notice_reported") is True

    asyncio.run(
        moderation._report_moderation_fallback_to_log(
            scanner,
            fallback_notice=fallback_notice,
            image_state=state,
            payload_metadata=metadata,
        )
    )
    assert captured["calls"] == 1
