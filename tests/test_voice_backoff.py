from __future__ import annotations

from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cogs.voice_moderation import backoff as backoff_module


def test_voice_backoff_escalates_and_clears(monkeypatch):
    current = [500.0]
    fake_time = types.SimpleNamespace(monotonic=lambda: current[0])
    monkeypatch.setattr(backoff_module, "time", fake_time)

    tracker = backoff_module.VoiceConnectBackoff(base_seconds=5.0, max_seconds=40.0)

    delay = tracker.record_failure(1, 2)
    assert delay == 5.0
    assert round(tracker.remaining(1, 2), 2) == 5.0

    delay = tracker.record_failure(1, 2)
    assert delay == 10.0
    remaining = tracker.remaining(1, 2)
    assert 9.0 <= remaining <= 10.0

    current[0] += 12.0
    assert tracker.remaining(1, 2) == 0.0

    tracker.record_failure(1, 2)
    tracker.clear(1, 2)
    assert tracker.remaining(1, 2) == 0.0
