import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.i18n.locales import LocaleRepository
from modules.i18n.translator import Translator

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

def test_refresh_reloads_from_disk(tmp_path: Path) -> None:
    root = tmp_path / "locales"
    _write(root, "en.json", {"value": "old"})

    repository = LocaleRepository(root, default_locale="en", fallback_locale="en")
    repository.reload()

    _write(root, "en.json", {"value": "new"})
    repository.refresh()

    assert repository.get_value("en", "value") == "new"
