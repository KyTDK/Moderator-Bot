from datetime import timedelta
import pytest
import sys
from pathlib import Path

# Ensure the project root is on the import path
sys.path.append(str(Path(__file__).resolve().parents[1]))

from modules.utils.time import parse_duration

@pytest.mark.parametrize(
    "input_str,expected",
    [
        ("10s", timedelta(seconds=10)),
        ("10second", timedelta(seconds=10)),
        ("10 second", timedelta(seconds=10)),
        ("10 seconds", timedelta(seconds=10)),
        ("5m", timedelta(minutes=5)),
        ("5min", timedelta(minutes=5)),
        ("5 min", timedelta(minutes=5)),
        ("5minute", timedelta(minutes=5)),
        ("5 minute", timedelta(minutes=5)),
        ("5 minutes", timedelta(minutes=5)),
        ("5 minu", None),
        ("2h", timedelta(hours=2)),
        ("3d", timedelta(days=3)),
        ("1w", timedelta(weeks=1)),
        ("2mo", timedelta(days=60)),
        ("1y", timedelta(days=365)),
        ("3months", timedelta(days=90)),
        ("3 months", timedelta(days=90))
    ],
)
def test_parse_duration_valid(input_str, expected):
    assert parse_duration(input_str) == expected


@pytest.mark.parametrize("input_str", ["5x", ""])
def test_parse_duration_invalid(input_str):
    assert parse_duration(input_str) is None
