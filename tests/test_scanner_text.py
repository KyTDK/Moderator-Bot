import asyncio
from dataclasses import dataclass
import enum
import pytest
import sys
from pathlib import Path
from types import SimpleNamespace
import types

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

# Minimal third-party stubs required for importing the scanner module.
apnggif_stub = types.ModuleType("apnggif")


def _apnggif(*_args, **_kwargs):
    return None


apnggif_stub.apnggif = _apnggif
sys.modules.setdefault("apnggif", apnggif_stub)

pillow_avif_stub = types.ModuleType("pillow_avif")
sys.modules.setdefault("pillow_avif", pillow_avif_stub)

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
        self.fields = []
        self.title = kwargs.get("title")
        self.description = kwargs.get("description")
        self.color = kwargs.get("color")
        self.footer = None

    def add_field(self, *args, **_kwargs):
        self.fields.append((args, _kwargs))
        return None

    def set_thumbnail(self, *args, **_kwargs):
        return None

    def set_image(self, *args, **_kwargs):
        return None

    def set_footer(self, *, text=None, icon_url=None):
        self.footer = {"text": text, "icon_url": icon_url}

    def copy(self):
        new_embed = _DummyEmbed(*self.args, **self.kwargs)
        new_embed.fields = list(self.fields)
        new_embed.title = self.title
        new_embed.description = self.description
        new_embed.color = self.color
        new_embed.footer = self.footer.copy() if self.footer else None
        return new_embed


class _DummyColor:
    @staticmethod
    def red():
        return 0xFF0000

    @staticmethod
    def orange():
        return 0xFFA500

    @staticmethod
    def dark_grey():
        return 0x2F3136


class _DummyFile:
    def __init__(self, *args, **_kwargs):
        pass


class _DummyAllowedMentions:
    @staticmethod
    def none():
        return None


discord_stub.Embed = _DummyEmbed
discord_stub.Color = _DummyColor
discord_stub.File = _DummyFile
discord_stub.AllowedMentions = _DummyAllowedMentions
discord_stub.Client = type("Client", (), {})
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
discord_utils_stub = types.ModuleType("discord.utils")


def _dummy_get(iterable, **attrs):
    return None


def _escape_markdown(value, *, as_needed=False):
    return value


def _escape_mentions(value):
    return value


discord_utils_stub.get = _dummy_get
discord_utils_stub.escape_markdown = _escape_markdown
discord_utils_stub.escape_mentions = _escape_mentions
sys.modules.setdefault("discord.utils", discord_utils_stub)

discord_stub.utils = discord_utils_stub
discord_stub.abc = types.SimpleNamespace(Messageable=object, User=object)

errors_stub = types.ModuleType("discord.errors")
errors_stub.NotFound = discord_stub.NotFound
errors_stub.Forbidden = discord_stub.Forbidden
errors_stub.HTTPException = discord_stub.HTTPException
sys.modules.setdefault("discord.errors", errors_stub)
discord_stub.errors = errors_stub

discord_ext_stub = types.ModuleType("discord.ext")
commands_stub = types.ModuleType("discord.ext.commands")
tasks_stub = types.ModuleType("discord.ext.tasks")


def _loop(*_args, **_kwargs):
    def _decorator(func):
        return func

    return _decorator


tasks_stub.loop = _loop
app_commands_module = types.ModuleType("discord.app_commands")
app_commands_module.CommandTree = object
app_commands_module.check = lambda func: func


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

sys.modules.setdefault("discord", discord_stub)
sys.modules.setdefault("discord.ext", discord_ext_stub)
sys.modules.setdefault("discord.ext.commands", commands_stub)
sys.modules.setdefault("discord.ext.tasks", tasks_stub)
sys.modules.setdefault("discord.app_commands", app_commands_module)

clip_vectors_stub = types.ModuleType("modules.utils.clip_vectors")
clip_vectors_stub.query_similar = lambda *_args, **_kwargs: []
clip_vectors_stub.delete_vectors = lambda *_args, **_kwargs: None
clip_vectors_stub.is_available = lambda: False
clip_vectors_stub.register_failure_callback = lambda *_args, **_kwargs: None
sys.modules.setdefault("modules.utils.clip_vectors", clip_vectors_stub)

text_vectors_stub = types.ModuleType("modules.utils.text_vectors")
text_vectors_stub.query_similar = lambda *_args, **_kwargs: []
text_vectors_stub.delete_vectors = lambda *_args, **_kwargs: None
text_vectors_stub.is_available = lambda: False
text_vectors_stub.register_failure_callback = lambda *_args, **_kwargs: None
sys.modules.setdefault("modules.utils.text_vectors", text_vectors_stub)

mysql_stub = types.ModuleType("modules.utils.mysql")


async def _unpatched_async(*_args, **_kwargs):
    raise AssertionError("mysql stub function should be monkeypatched by the test")


mysql_stub.get_settings = _unpatched_async
mysql_stub.resolve_guild_plan = _unpatched_async
mysql_stub.is_accelerated = _unpatched_async
mysql_stub.get_strike_count = _unpatched_async


@dataclass(slots=True)
class _StubShardAssignment:
    shard_id: int
    shard_count: int


class _StubShardClaimError(RuntimeError):
    pass


mysql_stub.ShardAssignment = _StubShardAssignment
mysql_stub.ShardClaimError = _StubShardClaimError
sys.modules.setdefault("modules.utils.mysql", mysql_stub)

mod_logging_stub = types.ModuleType("modules.utils.mod_logging")


async def _log_to_channel_stub(*_args, **_kwargs):
    return None


mod_logging_stub.log_to_channel = _log_to_channel_stub
sys.modules.setdefault("modules.utils.mod_logging", mod_logging_stub)

cache_stub = types.ModuleType("modules.cache")
async def _cache_async(*_args, **_kwargs):
    return None
cache_stub.get_cached_message = _cache_async
cache_stub.cache_message = _cache_async
sys.modules.setdefault("modules.cache", cache_stub)

filetype_stub = types.ModuleType("filetype")
filetype_stub.guess = lambda *_args, **_kwargs: None
sys.modules.setdefault("filetype", filetype_stub)

nsfw_utils_stub = types.ModuleType("modules.nsfw_scanner.utils")
nsfw_utils_stub.__path__ = [str(PROJECT_ROOT / "modules" / "nsfw_scanner" / "utils")]
sys.modules.setdefault("modules.nsfw_scanner.utils", nsfw_utils_stub)

frames_stub = types.ModuleType("modules.nsfw_scanner.utils.frames")
frames_stub.ExtractedFrame = object
frames_stub.iter_extracted_frames = lambda *_args, **_kwargs: iter(())
frames_stub.frames_are_similar = lambda *_args, **_kwargs: False
sys.modules.setdefault("modules.nsfw_scanner.utils.frames", frames_stub)

api_stub = types.ModuleType("modules.utils.api")


async def _api_get_api_client(_guild_id):
    return None, None


async def _api_set_api_key_not_working(*_args, **_kwargs):
    return None


async def _api_is_api_key_working(*_args, **_kwargs):
    return False


async def _api_set_api_key_working(*_args, **_kwargs):
    return None


api_stub.get_api_client = _api_get_api_client
api_stub.set_api_key_not_working = _api_set_api_key_not_working
api_stub.is_api_key_working = _api_is_api_key_working
api_stub.set_api_key_working = _api_set_api_key_working
sys.modules.setdefault("modules.utils.api", api_stub)

from modules.nsfw_scanner import scanner as scanner_mod
from modules.nsfw_scanner.helpers import attachments as attachments_mod
from modules.nsfw_scanner.settings_keys import (
    NSFW_HIGH_ACCURACY_SETTING,
    NSFW_IMAGE_CATEGORY_SETTING,
    NSFW_TEXT_ACTION_SETTING,
    NSFW_TEXT_CATEGORY_SETTING,
    NSFW_TEXT_ENABLED_SETTING,
    NSFW_TEXT_EXCLUDED_CHANNELS_SETTING,
    NSFW_TEXT_STRIKES_ONLY_SETTING,
    NSFW_TEXT_THRESHOLD_SETTING,
    NSFW_THRESHOLD_SETTING,
)


async def _exercise_text_scan(
    monkeypatch,
    *,
    accelerated_value: bool,
    verbose_value: bool = True,
    scan_media: bool = True,
    scan_text: bool = True,
    excluded_channels: list[int] | None = None,
):
    """Run the text scanning pipeline with controllable acceleration flag."""
    author = SimpleNamespace(
        id=42,
        mention="<@42>",
        display_name="TestUser",
        bot=False,
    )
    channel = SimpleNamespace(id=99)
    message = SimpleNamespace(
        content="http://example.com explicit content",
        attachments=[],
        embeds=[],
        stickers=[],
        message_snapshots=[],
        author=author,
        channel=channel,
        id=1234,
        reactions=[],
    )

    async def fake_wait_for_hydration(msg, *, timeout=4.0):
        return msg

    settings_payload = {
        NSFW_IMAGE_CATEGORY_SETTING: ["sexual"],
        NSFW_TEXT_CATEGORY_SETTING: ["sexual"],
        NSFW_THRESHOLD_SETTING: 0.7,
        NSFW_TEXT_THRESHOLD_SETTING: 0.7,
        NSFW_HIGH_ACCURACY_SETTING: False,
        NSFW_TEXT_ENABLED_SETTING: True,
        NSFW_TEXT_STRIKES_ONLY_SETTING: False,
        NSFW_TEXT_EXCLUDED_CHANNELS_SETTING: list(excluded_channels or []),
    }

    def fake_get_scan_settings(self):
        return settings_payload

    def fake_set_scan_settings(self, value):
        return None

    async def fake_is_accelerated(*, guild_id=None, user_id=None):
        return accelerated_value

    async def fake_get_strike_count(user_id, guild_id):
        return 1

    async def fake_get_settings(guild_id, keys=None):
        if keys is None:
            return settings_payload.copy()
        if isinstance(keys, list):
            return settings_payload.copy()
        if keys == "nsfw-verbose":
            return verbose_value
        if keys == NSFW_TEXT_ENABLED_SETTING:
            return settings_payload[NSFW_TEXT_ENABLED_SETTING]
        if keys == NSFW_TEXT_EXCLUDED_CHANNELS_SETTING:
            return settings_payload[NSFW_TEXT_EXCLUDED_CHANNELS_SETTING]
        return None

    text_calls = []

    async def fake_process_text(scanner_arg, text, **kwargs):
        text_calls.append((scanner_arg, text, kwargs))
        return {
            "is_nsfw": True,
            "category": "sexual",
            "score": 0.91,
        }

    callback_calls = []

    async def fake_callback(*args, **kwargs):
        callback_calls.append((args, kwargs))

    log_calls: list[dict[str, object]] = []

    async def fake_log_to_channel(embed, channel_id, bot, file=None):
        log_calls.append({"embed": embed, "channel_id": channel_id})

    log_embed_calls: list[dict[str, object]] = []

    async def fake_send_developer_log_embed(
        bot,
        *,
        content=None,
        embed=None,
        allowed_mentions=None,
        logger=None,
        context=None,
    ):
        log_embed_calls.append(
            {
                "content": content,
                "embed": embed,
                "context": context,
            }
        )
        return True

    monkeypatch.setattr(scanner_mod, "wait_for_hydration", fake_wait_for_hydration)
    monkeypatch.setattr(
        attachments_mod.AttachmentSettingsCache,
        "get_scan_settings",
        fake_get_scan_settings,
    )
    monkeypatch.setattr(
        attachments_mod.AttachmentSettingsCache,
        "set_scan_settings",
        fake_set_scan_settings,
    )
    monkeypatch.setattr(scanner_mod.mysql, "get_settings", fake_get_settings, raising=False)
    monkeypatch.setattr(scanner_mod.mysql, "is_accelerated", fake_is_accelerated, raising=False)
    monkeypatch.setattr(scanner_mod.mysql, "get_strike_count", fake_get_strike_count, raising=False)

    import modules.utils.mod_logging as mod_logging_module
    import modules.utils.log_channel as log_channel_module
    import modules.nsfw_scanner.text_pipeline as text_pipeline_module
    import modules.nsfw_scanner.constants as scanner_constants

    monkeypatch.setattr(scanner_constants, "LOG_CHANNEL_ID", 123, raising=False)
    monkeypatch.setattr(mod_logging_module, "log_to_channel", fake_log_to_channel, raising=False)
    monkeypatch.setattr(log_channel_module, "send_developer_log_embed", fake_send_developer_log_embed, raising=False)
    monkeypatch.setattr(text_pipeline_module, "send_developer_log_embed", fake_send_developer_log_embed, raising=False)
    monkeypatch.setattr(text_pipeline_module, "process_text", fake_process_text, raising=False)

    scanner = scanner_mod.NSFWScanner(bot=SimpleNamespace())
    scanner._text_pipeline = text_pipeline_module.TextScanPipeline(bot=scanner.bot)

    outcome = await scanner.is_nsfw(
        message=message,
        guild_id=555,
        nsfw_callback=fake_callback,
        scan_media=scan_media,
        scan_text=scan_text,
        return_details=True,
    )

    return (
        outcome["flagged"],
        text_calls,
        callback_calls,
        author,
        log_calls,
        log_embed_calls,
        outcome,
    )


def test_text_scan_does_not_log_without_verbose(monkeypatch):
    flagged, text_calls, callback_calls, _, log_calls, log_embed_calls, outcome = asyncio.run(
        _exercise_text_scan(monkeypatch, accelerated_value=True, verbose_value=False)
    )
    assert flagged is True
    assert text_calls, "process_text should run when text scanning is enabled"
    assert callback_calls, "Actions should fire when acceleration allows it"
    assert callback_calls[0][1]["send_embed"] is False
    assert not log_calls, "Verbose channel logging should be suppressed without nsfw-verbose"
    assert not log_embed_calls, "Debug logs should be suppressed without nsfw-verbose"
    assert outcome["text_flagged"] is True, "Scanner should report text-based hits"


def test_text_scan_skipped_when_channel_excluded(monkeypatch):
    flagged, text_calls, callback_calls, *_ = asyncio.run(
        _exercise_text_scan(
            monkeypatch,
            accelerated_value=True,
            excluded_channels=[99],
        )
    )
    assert not flagged, "Message should not be flagged when channel is excluded"
    assert not text_calls, "Text scanning should be skipped for excluded channels"
    assert not callback_calls, "No actions should fire when scanning is skipped"


def test_text_scan_runs_when_media_scanning_disabled(monkeypatch):
    flagged, text_calls, callback_calls, _, _, _, outcome = asyncio.run(
        _exercise_text_scan(
            monkeypatch,
            accelerated_value=True,
            scan_media=False,
            scan_text=True,
        )
    )
    assert flagged is True
    assert text_calls, "process_text should still run when media scanning is disabled"
    assert callback_calls, "Text actions should still fire when only text scanning is enabled"
    assert outcome["text_flagged"] is True, "Scanner should report text-based hits"
    assert callback_calls[0][1]["send_embed"] is False
