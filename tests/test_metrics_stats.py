import asyncio
import sys
from pathlib import Path

import pytest

project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.append(str(project_root))

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


def test_compute_latency_breakdown_handles_missing_frames(monkeypatch):
    async def fake_totals():
        return {"scans_count": 0}

    async def fake_summary():
        return [
            {"content_type": "video", "scans": 0, "total_duration_ms": None},
        ]

    monkeypatch.setattr("modules.metrics.stats.fetch_metric_totals", fake_totals)
    monkeypatch.setattr("modules.metrics.stats.summarise_rollups", fake_summary)

    breakdown = asyncio.run(compute_latency_breakdown())
    assert breakdown["video"]["average_latency_per_frame_ms"] is None


def test_compute_latency_breakdown_handles_partial_resets(monkeypatch):
    async def fake_totals():
        # Totals reset: counts remain but duration cleared.
        return {
            "scans_count": 50,
            "total_duration_ms": None,
            "total_frames_scanned": None,
        }

    async def fake_summary():
        return [
            {
                "content_type": "video",
                "scans": 30,
                "total_duration_ms": 0,
                "total_frames_scanned": 1200,
            },
            {
                "content_type": "image",
                "scans": 20,
                "total_duration_ms": None,
                "total_frames_scanned": None,
            },
        ]

    monkeypatch.setattr("modules.metrics.stats.fetch_metric_totals", fake_totals)
    monkeypatch.setattr("modules.metrics.stats.summarise_rollups", fake_summary)

    breakdown = asyncio.run(compute_latency_breakdown())
    assert breakdown["overall"]["average_latency_ms"] is None
    assert breakdown["video"]["average_latency_ms"] == 0.0
    assert breakdown["video"]["average_latency_per_frame_ms"] == 0.0
    assert breakdown["image"]["average_latency_ms"] is None


def test_compute_latency_breakdown_limits_extra_types(monkeypatch):
    async def fake_totals():
        return {
            "scans_count": 500,
            "total_duration_ms": 10000,
            "total_frames_scanned": 5000,
        }

    async def fake_summary():
        buckets = []
        for idx in range(10):
            buckets.append(
                {
                    "content_type": f"type_{idx}",
                    "scans": idx + 1,
                    "total_duration_ms": (idx + 1) * 100,
                    "total_frames_scanned": (idx + 1) * 50,
                }
            )
        return buckets

    monkeypatch.setattr("modules.metrics.stats.fetch_metric_totals", fake_totals)
    monkeypatch.setattr("modules.metrics.stats.summarise_rollups", fake_summary)

    breakdown = asyncio.run(compute_latency_breakdown())
    assert "overall" in breakdown
    assert len(breakdown["by_type"]) == 10
