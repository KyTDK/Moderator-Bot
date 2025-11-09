import asyncio
from unittest import mock

import pytest

from modules.metrics.stats import compute_latency_breakdown


def test_compute_latency_breakdown_uses_totals_and_summary(monkeypatch):
    async def fake_totals():
        return {
            "scans_count": 10,
            "total_duration_ms": 1000,
            "average_latency_per_frame_ms": 0.5,
            "total_frames_scanned": 2000,
        }

    async def fake_summary():
        return [
            {
                "content_type": "video",
                "scans": 6,
                "total_duration_ms": 900,
                "average_latency_per_frame_ms": 0.7,
                "total_frames_scanned": 1500,
            },
            {
                "content_type": "image",
                "scans": 4,
                "total_duration_ms": 100,
                "total_frames_scanned": 0,
            },
        ]

    monkeypatch.setattr("modules.metrics.stats.fetch_metric_totals", fake_totals)
    monkeypatch.setattr("modules.metrics.stats.summarise_rollups", fake_summary)

    breakdown = asyncio.run(compute_latency_breakdown())
    assert breakdown["overall"]["average_latency_ms"] == pytest.approx(100.0)
    assert breakdown["video"]["scans"] == 6
    assert breakdown["image"]["average_latency_ms"] == pytest.approx(25.0)
