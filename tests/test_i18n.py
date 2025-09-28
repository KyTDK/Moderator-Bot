import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.i18n import CrowdinConfigurationError, CrowdinSettings
from modules.i18n.locales import LocaleRepository
from modules.i18n.translator import Translator

class _DummyCrowdinService:
    def __init__(self, files: dict[str, dict]) -> None:
        self.files = files
        self.calls = 0

    def refresh_locales(self, destination: Path) -> Path:
        self.calls += 1
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        for relative, payload in self.files.items():
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(payload), encoding="utf-8")
        return destination

def _write(locale_root: Path, relative: str, payload: dict) -> None:
    target = locale_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload), encoding="utf-8")

def test_locale_repository_merges_nested_json(tmp_path: Path) -> None:
    root = tmp_path / "locales"
    _write(root, "en/general.json", {"greeting": "Hello {name}", "section": {"title": "Start"}})
    _write(root, "en/nested/info.json", {"section": {"subtitle": "Sub"}})
    _write(root, "fr.json", {"greeting": "Bonjour {name}"})

    repository = LocaleRepository(root, default_locale="en", fallback_locale="en")
    repository.reload()

    assert repository.list_locales() == ["en", "fr"]
    assert repository.get_value("en", "greeting") == "Hello {name}"
    assert repository.get_value("en", "section.title") == "Start"
    assert repository.get_value("en", "section.subtitle") == "Sub"
    assert repository.get_value("fr", "greeting") == "Bonjour {name}"

def test_translator_fallback_and_formatting(tmp_path: Path) -> None:
    root = tmp_path / "locales"
    _write(root, "en/strings.json", {"greeting": "Hello {name}"})
    _write(root, "en/status.json", {"status": {"ready": "Ready"}})
    _write(root, "es.json", {"greeting": "Hola {name}"})

    repository = LocaleRepository(root, default_locale="en", fallback_locale="en")
    repository.reload()

    translator = Translator(repository)

    assert translator.translate("greeting", locale="es", placeholders={"name": "Alex"}) == "Hola Alex"
    assert translator.translate("status.ready", locale="es") == "Ready"
    assert translator.translate("missing.key", fallback="Fallback") == "Fallback"

def test_refresh_uses_crowdin_service(tmp_path: Path) -> None:
    root = tmp_path / "locales"
    _write(root, "en.json", {"value": "old"})

    repository = LocaleRepository(root, default_locale="en", fallback_locale="en")
    repository.reload()

    service = _DummyCrowdinService({"en.json": {"value": "new"}})
    repository.refresh(service)

    assert service.calls == 1
    assert repository.get_value("en", "value") == "new"

def test_crowdin_settings_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CROWDIN_API_TOKEN", "token")
    monkeypatch.setenv("CROWDIN_PROJECT_ID", "123")
    monkeypatch.setenv("CROWDIN_LOCALES_DIR", str(tmp_path / "locales"))
    monkeypatch.setenv("CROWDIN_TARGET_LOCALES", "fr, es ")
    monkeypatch.setenv("CROWDIN_BRANCH", "main")

    settings = CrowdinSettings.from_env()

    assert settings.token == "token"
    assert settings.project_id == 123
    assert settings.locales_root == (tmp_path / "locales").resolve()
    assert settings.target_locales == ("fr", "es")
    assert settings.branch == "main"

    monkeypatch.delenv("CROWDIN_API_TOKEN")
    monkeypatch.delenv("CROWDIN_PROJECT_ID")
    with pytest.raises(CrowdinConfigurationError):
        CrowdinSettings.from_env()
