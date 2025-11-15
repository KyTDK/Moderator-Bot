import asyncio
import os
import importlib.util
import sys
import types
from pathlib import Path

import pytest

os.environ.setdefault("FERNET_SECRET_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")

if "psutil" not in sys.modules:
    class _DummyProcess:
        def __init__(self, *_, **__):
            pass

        def memory_info(self):
            return types.SimpleNamespace(rss=0, vms=0)

        def cpu_percent(self, interval=0.0):
            return 0.0

        def num_threads(self):
            return 0

        def num_handles(self):
            return 0

    dummy_psutil = types.SimpleNamespace(Process=_DummyProcess)
    dummy_psutil.__spec__ = importlib.util.spec_from_loader("psutil", loader=None)
    sys.modules["psutil"] = dummy_psutil

spec = importlib.util.spec_from_file_location(
    "debug_stats_test",
    Path(__file__).resolve().parents[1] / "cogs" / "debug" / "commands" / "stats.py",
)
debug_stats = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(debug_stats)

_build_latency_row = debug_stats._build_latency_row
_build_latency_table = debug_stats._build_latency_table
_format_latency_value = debug_stats._format_latency_value

from cogs.debug.commands.stats import _build_latency_row, _build_latency_table, _format_latency_value


def test_format_latency_value_handles_numbers_and_invalid_values():
    assert _format_latency_value(123.456) == "123.5"
    assert _format_latency_value("789.0") == "789.0"
    assert _format_latency_value(None) == "n/a"
    assert _format_latency_value("not-a-number") == "n/a"


def test_build_latency_row_formats_missing_values():
    payload = {
        "average_latency_ms": 150.0,
        "acceleration": {
            "non_accelerated": {"average_latency_ms": 200.0},
            "accelerated": {"average_latency_ms": None},
        },
    }
    row = _build_latency_row("Video", payload)
    assert row == ("Video", "150.0", "200.0", "n/a")

    assert _build_latency_row("Image", None) is None


def test_build_latency_table_renders_sorted_rows(monkeypatch):
    breakdown = {
        "overall": {
            "average_latency_ms": 80.5,
            "acceleration": {
                "non_accelerated": {"average_latency_ms": 69.4},
                "accelerated": {"average_latency_ms": 111.0},
            },
        },
        "by_type": {
            "video": {
                "label": "video",
                "scans": 50,
                "average_latency_ms": 100.0,
                "acceleration": {
                    "non_accelerated": {"average_latency_ms": 95.0},
                    "accelerated": {"average_latency_ms": 120.0},
                },
            },
            "image": {
                "label": "image",
                "scans": 10,
                "average_latency_ms": 40.0,
                "acceleration": {
                    "non_accelerated": {"average_latency_ms": 30.0},
                    "accelerated": {"average_latency_ms": 60.0},
                },
            },
        },
    }

    async def fake_breakdown():
        return breakdown

    monkeypatch.setattr("cogs.debug.commands.stats.compute_latency_breakdown", fake_breakdown)

    table = asyncio.run(_build_latency_table())

    lines = table.splitlines()
    assert lines[0].startswith("Type")
    assert "Overall" in lines[1]
    assert lines[2].startswith("Video")
    assert lines[3].startswith("Image")
    assert "120.0" in table  # Accelerated latency value shows up in table.
