from __future__ import annotations

from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.core.moderator_bot import connection_health


def test_gateway_monitor_threshold_and_cooldown(monkeypatch):
    current = [1_000.0]
    fake_time = types.SimpleNamespace(monotonic=lambda: current[0])
    monkeypatch.setattr(connection_health, "time", fake_time)

    monitor = connection_health.GatewayHealthMonitor(
        threshold=2,
        window_seconds=60.0,
        cooldown_seconds=30.0,
    )

    assert monitor.record_disconnect() is None

    current[0] += 5.0
    snapshot = monitor.record_disconnect()
    assert snapshot is not None
    assert snapshot.disconnect_count == 2
    assert snapshot.first_disconnect_age == 5.0

    # Cooldown prevents immediate duplicate alerts
    current[0] += 1.0
    assert monitor.record_disconnect() is None

    # After cooldown expires, hitting the threshold again returns a snapshot
    current[0] += 35.0
    snapshot = monitor.record_disconnect()
    assert snapshot is not None
