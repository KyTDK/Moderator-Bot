from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault(
    "FERNET_SECRET_KEY", "DeJ3sXDDTTbikeRSJzRgg8r_Ch61_NbE8D3LWnLOJO4="
)
sys.path.append(str(Path(__file__).resolve().parents[1]))

from modules.config.settings_schema import SETTINGS_SCHEMA
from modules.i18n.locale_utils import list_supported_locales
from modules.core.moderator_bot import ModeratorBot, _current_locale
from modules.utils import mysql


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
    yield bot
    mysql.remove_settings_listener(bot._locale_settings_listener)


def test_interaction_locale_uses_preferred_locale(bot: ModeratorBot) -> None:
    guild = DummyGuild(id=123, preferred_locale="en-US")
    interaction = DummyInteraction(
        guild=guild,
        locale=None,
        guild_id=guild.id,
    )

    resolved = bot._infer_locale_from_event("interaction_create", (interaction,), {})

    assert resolved == "en"


def test_context_uses_guild_preference(bot: ModeratorBot) -> None:
    guild = DummyGuild(id=456, preferred_locale="es-ES")
    ctx = DummyContext(guild=guild)

    resolved = bot._infer_locale_from_event("command", (ctx,), {})

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

    resolved = bot._infer_locale_from_event("interaction", (interaction,), {})

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

    resolved = bot._infer_locale_from_event("interaction", (interaction,), {})

    assert resolved == "pt-BR"

def test_unsupported_locale_rejects_value(bot: ModeratorBot) -> None:
    result = bot.translate("bot.welcome.button_label", locale="zz-ZZ")

    assert result == "Open Dashboard"

def test_translate_defaults_without_context(bot: ModeratorBot) -> None:
    result = bot.translate("bot.welcome.button_label")

    assert result == "Open Dashboard"

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

def test_locale_context_manager_restores_previous_locale(bot: ModeratorBot) -> None:
    with bot.locale_context("fr-FR"):
        assert _current_locale.get() == "fr-FR"

    assert _current_locale.get() is None

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

    try:
        expected_root = Path(__file__).resolve().parents[1] / "locales"
        assert bot.locale_repository.locales_root == expected_root.resolve()
    finally:
        mysql.remove_settings_listener(bot._locale_settings_listener)
        monkeypatch.delenv("I18N_LOCALES_DIR", raising=False)
