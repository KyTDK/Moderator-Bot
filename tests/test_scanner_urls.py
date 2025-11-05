import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

import tests.test_scanner_text  # noqa: F401 - ensures dependency stubs are registered
import modules.nsfw_scanner.scanner as scanner_mod


def test_normalize_source_url_strips_spoiler_wrappers():
    normalize = scanner_mod.NSFWScanner._normalize_source_url
    raw = "||https://example.com/image.png?width=10&height=20||"
    assert normalize(raw) == "https://example.com/image.png?width=10&height=20"


def test_normalize_source_url_strips_angle_brackets_and_combination():
    normalize = scanner_mod.NSFWScanner._normalize_source_url
    wrapped = "||<https://example.com/path>||"
    assert normalize(wrapped) == "https://example.com/path"


def test_normalize_source_url_returns_none_when_only_markup():
    normalize = scanner_mod.NSFWScanner._normalize_source_url
    assert normalize("||  ||") is None
    assert normalize("   ") is None


def test_normalize_source_url_leaves_regular_url_untouched():
    normalize = scanner_mod.NSFWScanner._normalize_source_url
    url = "https://cdn.discordapp.com/attachments/123/file.jpg"
    assert normalize(url) == url
