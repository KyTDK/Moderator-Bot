from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.i18n.guild_cache import extract_guild_id


def test_extracts_from_mapping_guild_id() -> None:
    payload = {"guild_id": "12345"}

    assert extract_guild_id(payload) == 12345


def test_extracts_from_nested_mapping() -> None:
    payload = {"guild": {"id": "67890"}}

    assert extract_guild_id(payload) == 67890


def test_extracts_from_guild_like_mapping_id() -> None:
    payload = {"id": "54321", "preferred_locale": "en-US", "name": "Example"}

    assert extract_guild_id(payload) == 54321


def test_ignores_unrelated_values() -> None:
    payload = {"id": "11111", "type": 0, "content": "hello"}

    assert extract_guild_id(payload) is None


def test_string_candidates_return_none() -> None:
    assert extract_guild_id("MESSAGE_CREATE") is None
