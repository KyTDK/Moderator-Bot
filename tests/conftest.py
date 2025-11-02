import enum
import importlib
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("MYSQL_FAKE", "1")


def _ensure_discord_locale() -> None:
    try:
        import discord
    except ModuleNotFoundError:  # pragma: no cover - discord not installed in test env
        return

    if not hasattr(discord, "Locale"):
        try:
            from discord.enums import Locale as _Locale  # type: ignore
        except Exception:  # pragma: no cover - legacy discord builds
            class _Locale(str):
                __slots__ = ("value",)

                def __new__(cls, value: str) -> "_Locale":
                    instance = str.__new__(cls, value)
                    instance.value = value
                    return instance

            for name, value in {
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
                setattr(_Locale, name, _Locale(value))
        else:
            _Locale = _Locale

        discord.Locale = _Locale
        if hasattr(discord, "__all__"):
            discord.__all__ = tuple(discord.__all__) + ("Locale",)

    try:
        app_commands = discord.app_commands  # type: ignore[attr-defined]
    except AttributeError:
        try:
            app_commands = importlib.import_module("discord.app_commands")
        except ModuleNotFoundError:  # pragma: no cover - legacy fallback
            app_commands = SimpleNamespace()
        discord.app_commands = app_commands  # type: ignore[attr-defined]

    if not hasattr(app_commands, "Translator"):
        class _Translator:
            async def load(self) -> None:  # pragma: no cover - compatibility shim
                return None

            async def translate(self, *args, **kwargs):  # pragma: no cover - compatibility shim
                raise NotImplementedError

        app_commands.Translator = _Translator  # type: ignore[attr-defined]

    if not hasattr(app_commands, "TranslationContextLocation"):
        class TranslationContextLocation(enum.Enum):
            command_name = enum.auto()
            command_description = enum.auto()
            group_name = enum.auto()
            group_description = enum.auto()
            parameter_name = enum.auto()
            parameter_description = enum.auto()
            choice_name = enum.auto()

        app_commands.TranslationContextLocation = TranslationContextLocation  # type: ignore[attr-defined]

    if not hasattr(app_commands, "TranslationContext"):
        class TranslationContext:
            def __init__(self, *, location, data=None):
                self.location = location
                self.data = data

        app_commands.TranslationContext = TranslationContext  # type: ignore[attr-defined]

    if not hasattr(app_commands, "locale_str"):
        class _LocaleStr(str):
            __slots__ = ("extras", "message")

            def __new__(cls, value: str, **extras):
                instance = str.__new__(cls, value)
                instance.extras = extras
                instance.message = value
                return instance

        def locale_str(value: str, /, **extras):
            return _LocaleStr(value, **extras)

        app_commands.locale_str = locale_str  # type: ignore[attr-defined]


def _restore_real_modules() -> None:
    for name in list(sys.modules):
        if name == "discord" or name.startswith("discord."):
            sys.modules.pop(name, None)
    sys.modules.pop("modules.utils.mysql", None)
    importlib.invalidate_caches()
    _ensure_discord_locale()
    importlib.import_module("modules.utils.mysql")


_ensure_discord_locale()


def pytest_runtest_setup(item):
    if item.fspath.basename == "test_moderator_bot_locale.py":
        _restore_real_modules()
