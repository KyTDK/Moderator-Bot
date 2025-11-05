import base64
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

os.environ.setdefault("FERNET_SECRET_KEY", base64.urlsafe_b64encode(b"0" * 32).decode())

from cogs.faq.cog import _parse_bool_setting


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        ("true", True),
        ("false", False),
        ("YES", True),
        ("off", False),
        (1, True),
        (0, False),
        ("", False),
    ],
)
def test_parse_bool_setting_truthy_falsy(value, expected):
    assert _parse_bool_setting(value, default=False) is expected


def test_parse_bool_setting_uses_default_for_none():
    assert _parse_bool_setting(None, default=True) is True
