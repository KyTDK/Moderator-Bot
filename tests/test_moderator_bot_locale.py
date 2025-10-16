from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from discord import Locale, app_commands

os.environ.setdefault(
    "FERNET_SECRET_KEY", "DeJ3sXDDTTbikeRSJzRgg8r_Ch61_NbE8D3LWnLOJO4="
)
sys.path.append(str(Path(__file__).resolve().parents[1]))

from modules.config.settings_schema import SETTINGS_SCHEMA
from modules.i18n.locale_utils import list_supported_locales
from modules.core.moderator_bot import ModeratorBot
from modules.utils import mysql
from modules.i18n.discord_translator import DiscordAppCommandTranslator
from modules.i18n.strings import locale_string


class DummyGuild(SimpleNamespace):
    id: int
    preferred_locale: str | None


class DummyContext(SimpleNamespace):
    guild: DummyGuild | None


class DummyInteraction(SimpleNamespace):
    guild: DummyGuild | None
    locale: str | None
    guild_id: int | None


@pytest.fixture
def bot(monkeypatch: pytest.MonkeyPatch) -> ModeratorBot:
    async def fake_get_guild_locale(_: int) -> str | None:
        return None

    monkeypatch.setattr(mysql, "get_guild_locale", fake_get_guild_locale)

    bot = ModeratorBot(
        instance_id="test",
        heartbeat_seconds=60,
        instance_heartbeat_seconds=5,
        log_cog_loads=False,
        total_shards=1,
    )
    asyncio.run(bot.ensure_i18n_ready())
    yield bot
    mysql.remove_settings_listener(bot._locale_settings_listener)


def test_interaction_locale_uses_preferred_locale(
    bot: ModeratorBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    guild = DummyGuild(id=123, preferred_locale="en-US")
    interaction = DummyInteraction(
        guild=guild,
        locale=None,
        guild_id=guild.id,
    )

    async def fake_get_settings(guild_id: int, key: str) -> str | None:
        assert guild_id == guild.id
        assert key == "locale"
        return None

    async def fake_get_guild_locale(guild_id: int) -> str | None:
        assert guild_id == guild.id
        return "en-US"

    monkeypatch.setattr(mysql, "get_settings", fake_get_settings)
    monkeypatch.setattr(mysql, "get_guild_locale", fake_get_guild_locale)

    asyncio.run(bot.refresh_guild_locale_override(guild.id))

    resolved = bot.resolve_locale(interaction)

    assert resolved == "en"


def test_context_uses_guild_preference(
    bot: ModeratorBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    guild = DummyGuild(id=456, preferred_locale="es-ES")
    ctx = DummyContext(guild=guild)

    async def fake_get_settings(guild_id: int, key: str) -> str | None:
        assert guild_id == guild.id
        assert key == "locale"
        return None

    async def fake_get_guild_locale(guild_id: int) -> str | None:
        assert guild_id == guild.id
        return "es-ES"

    monkeypatch.setattr(mysql, "get_settings", fake_get_settings)
    monkeypatch.setattr(mysql, "get_guild_locale", fake_get_guild_locale)

    asyncio.run(bot.refresh_guild_locale_override(guild.id))

    resolved = bot.resolve_locale(ctx)

    assert resolved == "es-ES"


def test_guild_override_has_priority(
    bot: ModeratorBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_get_settings(guild_id: int, key: str) -> str:
        assert guild_id == 789
        assert key == "locale"
        return "fr-FR"

    monkeypatch.setattr(mysql, "get_settings", fake_get_settings)

    asyncio.run(bot.refresh_guild_locale_override(789))

    guild = DummyGuild(id=789, preferred_locale="en-US")
    interaction = DummyInteraction(
        guild=guild,
        locale=None,
        guild_id=guild.id,
    )

    resolved = bot.resolve_locale(interaction)

    assert resolved == "fr-FR"

def test_falls_back_to_guild_table_locale(
    bot: ModeratorBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_get_settings(guild_id: int, key: str) -> str | None:
        assert guild_id == 321
        assert key == "locale"
        return None

    async def fake_get_guild_locale(guild_id: int) -> str | None:
        assert guild_id == 321
        return "pt-BR"

    monkeypatch.setattr(mysql, "get_settings", fake_get_settings)
    monkeypatch.setattr(mysql, "get_guild_locale", fake_get_guild_locale)

    asyncio.run(bot.refresh_guild_locale_override(321))

    guild = DummyGuild(id=321, preferred_locale="en-US")
    interaction = DummyInteraction(
        guild=guild,
        locale=None,
        guild_id=guild.id,
    )

    resolved = bot.resolve_locale(interaction)

    assert resolved == "pt-BR"

def test_unsupported_locale_rejects_value(bot: ModeratorBot) -> None:
    result = bot.translate("bot.welcome.button_label", locale="zz-ZZ")

    assert result == "Open Dashboard"

def test_translate_defaults_without_context(bot: ModeratorBot) -> None:
    result = bot.translate("bot.welcome.button_label")

    assert result == "Open Dashboard"


def test_translate_uses_guild_id_override(
    bot: ModeratorBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    guild_id = 987

    async def fake_get_settings(gid: int, key: str) -> str:
        assert gid == guild_id
        assert key == "locale"
        return "pl-PL"

    monkeypatch.setattr(mysql, "get_settings", fake_get_settings)

    asyncio.run(bot.refresh_guild_locale_override(guild_id))

    result = bot.translate("bot.welcome.button_label", guild_id=guild_id)

    assert result == LOCALIZED_WELCOME_LABELS["pl-PL"]


def test_use_locale_context_manager(bot: ModeratorBot) -> None:
    with bot.use_locale("es"):
        result = bot.translate("bot.welcome.button_label")

    assert result == "Abrir Panel de control"
    assert bot.translate("bot.welcome.button_label") == "Open Dashboard"


def test_push_and_reset_locale(bot: ModeratorBot) -> None:
    token = bot.push_locale("fr")
    try:
        assert bot.current_locale() == "fr-FR"
        assert bot.translate("bot.welcome.button_label") == "Ouvrir le tableau de bord"
    finally:
        bot.reset_locale(token)

    assert bot.current_locale() is None



def test_discord_translator_uses_locale_extras(bot: ModeratorBot) -> None:
    translator = DiscordAppCommandTranslator(bot.translation_service)
    locale_entry = locale_string("cogs.dashboard.meta.dashboard.description")
    context = app_commands.TranslationContext(
        location=app_commands.TranslationContextLocation.command_description,
        data=None,
    )

    result = asyncio.run(translator.translate(locale_entry, Locale.french, context))

    assert result == "Ouvrez le tableau de bord pour ce serveur."


def test_preload_guild_locale_cache(bot: ModeratorBot, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_all_guild_locales() -> dict[int, str | None]:
        return {111: "fr-FR", 222: None}

    monkeypatch.setattr(mysql, "get_all_guild_locales", fake_get_all_guild_locales)

    asyncio.run(bot._preload_guild_locale_cache())

    assert bot._guild_locales.get(111) == "fr-FR"
    assert bot._guild_locales.get(222) is None

LOCALIZED_WELCOME_LABELS: dict[str, str] = {
    "es-ES": "Abrir Panel de control",
    "fr-FR": "Ouvrir le tableau de bord",
    "pl-PL": "Otwórz Panel",
    "pt-PT": "Abrir Painel de Controle",
    "ru-RU": "Открыть панель управления",
    "sv-SE": "Öppna Instrumentpanel",
    "vi-VN": "Mở bảng điều khiển",
    "zh-CN": "打开仪表板",
}

LOCALE_ALIAS_EXPECTATIONS = [
    *[(canonical, label) for canonical, label in LOCALIZED_WELCOME_LABELS.items()],
    ("es", LOCALIZED_WELCOME_LABELS["es-ES"]),
    ("es-419", LOCALIZED_WELCOME_LABELS["es-ES"]),
    ("fr", LOCALIZED_WELCOME_LABELS["fr-FR"]),
    ("pl", LOCALIZED_WELCOME_LABELS["pl-PL"]),
    ("pt", LOCALIZED_WELCOME_LABELS["pt-PT"]),
    ("ru", LOCALIZED_WELCOME_LABELS["ru-RU"]),
    ("sv", LOCALIZED_WELCOME_LABELS["sv-SE"]),
    ("vi", LOCALIZED_WELCOME_LABELS["vi-VN"]),
    ("zh", LOCALIZED_WELCOME_LABELS["zh-CN"]),
]

@pytest.mark.parametrize("locale_hint,expected", LOCALE_ALIAS_EXPECTATIONS)
def test_locale_aliases_use_translated_welcome_button(
    bot: ModeratorBot, locale_hint: str, expected: str
) -> None:
    result = bot.translate("bot.welcome.button_label", locale=locale_hint)

    assert result == expected


def test_missing_translation_for_non_default_locale_logs_debug(
    bot: ModeratorBot, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="modules.i18n.translator")

    key = "cogs.settings.meta.help.options.unknown"
    fallback = "Example fallback"
    result = bot.translate(key, locale="fr-FR", fallback=fallback)

    assert result == fallback

    translator_records = [
        record
        for record in caplog.records
        if record.name == "modules.i18n.translator"
    ]
    assert translator_records
    assert any("missing" in record.getMessage() for record in translator_records)


def _spanish_settings_header() -> str:
    return "**Configuraciones Disponibles:**"


def test_settings_override_uses_spanish_translations(
    bot: ModeratorBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    guild = DummyGuild(id=555, preferred_locale="en-US")
    interaction = DummyInteraction(guild=guild, locale="en-US", guild_id=guild.id)

    async def fake_get_settings(guild_id: int, key: str) -> str:
        assert guild_id == guild.id
        assert key == "locale"
        return "es-ES"

    async def fake_get_guild_locale(_: int) -> str | None:
        return None

    monkeypatch.setattr(mysql, "get_settings", fake_get_settings)
    monkeypatch.setattr(mysql, "get_guild_locale", fake_get_guild_locale)

    asyncio.run(bot.refresh_guild_locale_override(guild.id))

    resolved = bot.resolve_locale(interaction)
    assert resolved == "es-ES"

    texts = bot.translate("cogs.settings.help", locale=resolved)

    assert isinstance(texts, dict)
    header = texts["header"]
    assert isinstance(header, str)
    assert header.startswith(_spanish_settings_header())
    assert "Available Settings" not in header


def test_settings_alias_override_normalises_to_spanish(
    bot: ModeratorBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    guild = DummyGuild(id=556, preferred_locale="en-US")
    interaction = DummyInteraction(guild=guild, locale="en-US", guild_id=guild.id)

    async def fake_get_settings(guild_id: int, key: str) -> str:
        assert guild_id == guild.id
        assert key == "locale"
        return "es"

    async def fake_get_guild_locale(_: int) -> str | None:
        return None

    monkeypatch.setattr(mysql, "get_settings", fake_get_settings)
    monkeypatch.setattr(mysql, "get_guild_locale", fake_get_guild_locale)

    asyncio.run(bot.refresh_guild_locale_override(guild.id))

    resolved = bot.resolve_locale(interaction)
    assert resolved == "es-ES"

    texts = bot.translate("cogs.settings.help", locale=resolved)

    assert isinstance(texts, dict)
    header = texts["header"]
    assert isinstance(header, str)
    assert header.startswith(_spanish_settings_header())
    assert "Available Settings" not in header


def test_invalid_override_falls_back_to_stored_locale(
    bot: ModeratorBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    guild = DummyGuild(id=557, preferred_locale="en-US")
    interaction = DummyInteraction(guild=guild, locale="en-US", guild_id=guild.id)

    async def fake_get_settings(guild_id: int, key: str) -> str:
        assert guild_id == guild.id
        assert key == "locale"
        return "zz-ZZ"

    async def fake_get_guild_locale(guild_id: int) -> str:
        assert guild_id == guild.id
        return "fr-FR"

    monkeypatch.setattr(mysql, "get_settings", fake_get_settings)
    monkeypatch.setattr(mysql, "get_guild_locale", fake_get_guild_locale)

    asyncio.run(bot.refresh_guild_locale_override(guild.id))

    resolved = bot.resolve_locale(interaction)
    assert resolved == "fr-FR"

    texts = bot.translate("cogs.settings.help", locale=resolved)

    assert isinstance(texts, dict)
    header = texts["header"]
    assert isinstance(header, str)
    assert header.startswith("**Paramètres disponibles :**")
    assert "Available Settings" not in header

def test_settings_override_precedes_interaction_locale(
    bot: ModeratorBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    guild = DummyGuild(id=654, preferred_locale="en-US")
    interaction = DummyInteraction(
        guild=guild,
        locale="en-US",
        guild_id=guild.id,
        user_locale="en-US",
    )

    async def fake_get_settings(guild_id: int, key: str) -> str:
        assert guild_id == guild.id
        assert key == "locale"
        return "es"

    monkeypatch.setattr(mysql, "get_settings", fake_get_settings)

    asyncio.run(bot.refresh_guild_locale_override(guild.id))

    resolution = bot.infer_locale(interaction)

    assert resolution.override == "es-ES"
    assert resolution.resolved() == "es-ES"
    assert resolution.source() == "override"

    translated = bot.translate(
        "bot.welcome.button_label", locale=resolution.resolved()
    )

    assert translated == "Abrir Panel de control"


def test_infer_locale_detects_from_interaction_when_no_override(bot: ModeratorBot) -> None:
    guild = DummyGuild(id=987, preferred_locale="en-US")
    interaction = DummyInteraction(
        guild=guild,
        locale="fr",
        guild_id=guild.id,
        user_locale="fr",
    )

    resolution = bot.infer_locale(interaction)

    assert resolution.override is None
    assert resolution.stored is None
    assert resolution.detected == "fr-FR"
    assert resolution.resolved() == "fr-FR"


def test_infer_locale_falls_back_to_stored_when_available(
    bot: ModeratorBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    guild = DummyGuild(id=3210, preferred_locale="en-US")
    ctx = DummyContext(guild=guild)

    async def fake_get_settings(guild_id: int, key: str) -> str | None:
        assert guild_id == guild.id
        assert key == "locale"
        return None

    async def fake_get_guild_locale(guild_id: int) -> str | None:
        assert guild_id == guild.id
        return "pt-BR"

    monkeypatch.setattr(mysql, "get_settings", fake_get_settings)
    monkeypatch.setattr(mysql, "get_guild_locale", fake_get_guild_locale)

    asyncio.run(bot.refresh_guild_locale_override(guild.id))

    resolution = bot.infer_locale(ctx)

    assert resolution.override is None
    assert resolution.stored == "pt-BR"
    assert resolution.resolved() == "pt-BR"


def test_get_guild_locale_returns_override(
    bot: ModeratorBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_get_settings(guild_id: int, key: str) -> str:
        assert guild_id == 999
        assert key == "locale"
        return "es-ES"

    async def fake_get_guild_locale(_: int) -> str | None:
        return "fr-FR"

    monkeypatch.setattr(mysql, "get_settings", fake_get_settings)
    monkeypatch.setattr(mysql, "get_guild_locale", fake_get_guild_locale)

    asyncio.run(bot.refresh_guild_locale_override(999))

    assert bot.get_guild_locale(999) == "es-ES"


def test_get_guild_locale_falls_back_to_stored(
    bot: ModeratorBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_get_settings(guild_id: int, key: str) -> str | None:
        assert guild_id == 1000
        assert key == "locale"
        return None

    async def fake_get_guild_locale(_: int) -> str | None:
        return "pt-BR"

    monkeypatch.setattr(mysql, "get_settings", fake_get_settings)
    monkeypatch.setattr(mysql, "get_guild_locale", fake_get_guild_locale)

    asyncio.run(bot.refresh_guild_locale_override(1000))

    assert bot.get_guild_locale(1000) == "pt-BR"

@pytest.mark.parametrize("candidate", ["fr-FR", "vi-VN"])
def test_locale_setting_validator_accepts_supported_locale(candidate: str) -> None:
    asyncio.run(SETTINGS_SCHEMA["locale"].validate(candidate))


def test_locale_setting_validator_rejects_unknown_locale() -> None:
    with pytest.raises(ValueError):
        asyncio.run(SETTINGS_SCHEMA["locale"].validate("zz-ZZ"))


def test_locale_setting_validator_rejects_alias_locales() -> None:
    with pytest.raises(ValueError):
        asyncio.run(SETTINGS_SCHEMA["locale"].validate("vi"))


def test_locale_setting_choices_list_supported_locales() -> None:
    assert SETTINGS_SCHEMA["locale"].choices == list_supported_locales()


def test_initialises_with_bundled_locales_when_config_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("I18N_LOCALES_DIR", "missing_locales")

    bot = ModeratorBot(
        instance_id="test",
        heartbeat_seconds=60,
        instance_heartbeat_seconds=5,
        log_cog_loads=False,
        total_shards=1,
    )
    asyncio.run(bot.ensure_i18n_ready())

    try:
        expected_root = Path(__file__).resolve().parents[1] / "locales"
        assert bot.locale_repository.locales_root == expected_root.resolve()
    finally:
        mysql.remove_settings_listener(bot._locale_settings_listener)
        monkeypatch.delenv("I18N_LOCALES_DIR", raising=False)


def test_translate_prefers_base_locale_before_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    locales_root = tmp_path / "custom_locales"
    (locales_root / "en").mkdir(parents=True)
    (locales_root / "es").mkdir(parents=True)
    (locales_root / "es-ES").mkdir(parents=True)

    (locales_root / "en/messages.json").write_text(
        json.dumps({"label": "Dashboard"}),
        encoding="utf-8",
    )
    (locales_root / "es/messages.json").write_text(
        json.dumps({"label": "Panel"}),
        encoding="utf-8",
    )
    (locales_root / "es-ES/messages.json").write_text(
        json.dumps({"welcome": "Hola"}),
        encoding="utf-8",
    )

    monkeypatch.setenv("I18N_LOCALES_DIR", str(locales_root))

    bot = ModeratorBot(
        instance_id="test",
        heartbeat_seconds=60,
        instance_heartbeat_seconds=5,
        log_cog_loads=False,
        total_shards=1,
    )
    asyncio.run(bot.ensure_i18n_ready())

    try:
        assert bot.translate("label", locale="es-ES") == "Panel"
    finally:
        mysql.remove_settings_listener(bot._locale_settings_listener)
        monkeypatch.delenv("I18N_LOCALES_DIR", raising=False)
