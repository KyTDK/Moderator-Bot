import asyncio
import sys
from pathlib import Path

import pytest

project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.append(str(project_root))

from modules.metrics.stats import LatencyStats, compute_latency_breakdown


def test_compute_latency_breakdown_uses_totals_and_summary(monkeypatch):
    async def fake_totals():
        return {
            "scans_count": 10,
            "total_duration_ms": 1000,
            "average_latency_per_frame_ms": 0.5,
            "total_frames_scanned": 2000,
            "acceleration": {
                "accelerated": {
                    "scans_count": 6,
                    "total_duration_ms": 600,
                    "total_frames_scanned": 1500,
                },
                "non_accelerated": {
                    "scans_count": 4,
                    "total_duration_ms": 400,
                    "average_latency_per_frame_ms": 0.4,
                    "total_frames_scanned": 500,
                },
            },
        }

    async def fake_summary():
        return [
            {
                "content_type": "video",
                "scans": 6,
                "duration_total_ms": 900,
                "average_latency_per_frame_ms": 0.7,
                "frames_total_scanned": 1500,
                "acceleration": {
                    "accelerated": {
                        "scans_count": 4,
                        "total_duration_ms": 600,
                        "total_frames_scanned": 1200,
                    },
                    "non_accelerated": {
                        "scans": 2,
                        "duration_total_ms": 300,
                        "frames_total_scanned": 300,
                    },
                },
            },
            {
                "content_type": "image",
                "scans": 4,
                "duration_total_ms": 100,
                "frames_total_scanned": 0,
                "acceleration": {
                    "non_accelerated": {
                        "scans": 4,
                        "duration_total_ms": 100,
                        "frames_total_scanned": 0,
                    }
                },
            },
        ]

    monkeypatch.setattr("modules.metrics.stats.fetch_metric_totals", fake_totals)
    monkeypatch.setattr("modules.metrics.stats.summarise_rollups", fake_summary)

    breakdown = asyncio.run(compute_latency_breakdown())
    assert breakdown["overall"]["average_latency_ms"] == pytest.approx(100.0)
    assert breakdown["video"]["scans"] == 6
    assert breakdown["image"]["average_latency_ms"] == pytest.approx(25.0)
    overall_accel = breakdown["overall"]["acceleration"]
    assert overall_accel["accelerated"]["average_latency_ms"] == pytest.approx(100.0)
    assert overall_accel["non_accelerated"]["average_latency_ms"] == pytest.approx(100.0)
    video_accel = breakdown["video"]["acceleration"]
    assert video_accel["accelerated"]["average_latency_ms"] == pytest.approx(150.0)
    assert video_accel["non_accelerated"]["average_latency_per_frame_ms"] == pytest.approx(1.0)
    image_accel = breakdown["image"]["acceleration"]
    assert image_accel["non_accelerated"]["scans"] == 4


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
                    "duration_total_ms": (idx + 1) * 100,
                    "frames_total_scanned": (idx + 1) * 50,
                }
            )
        return buckets

    monkeypatch.setattr("modules.metrics.stats.fetch_metric_totals", fake_totals)
    monkeypatch.setattr("modules.metrics.stats.summarise_rollups", fake_summary)

    breakdown = asyncio.run(compute_latency_breakdown())
    assert breakdown["overall"]["average_latency_ms"] == pytest.approx(20.0)
    assert len(breakdown["by_type"]) == 10


def test_compute_latency_breakdown_handles_aliases_and_missing_keys(monkeypatch):
    async def fake_totals():
        return {
            "scans": 4,
            "duration_total_ms": 800,
            "frames_total_scanned": 200,
            "acceleration": {
                "accelerated": {
                    "scans_count": 1,
                    "total_duration_ms": 200,
                    "total_frames_scanned": 50,
                },
                "non_accelerated": {
                    "scans": 3,
                    "duration_total_ms": 600,
                    "frames_total_scanned": 150,
                },
            },
        }

    async def fake_summary():
        return [
            {
                "content_type": "unknown",
                "scans_count": 4,
                "total_duration_ms": 800,
                "total_frames_scanned": 200,
                # Intentionally omit average_latency_per_frame_ms to force derived calc.
            }
        ]

    monkeypatch.setattr("modules.metrics.stats.fetch_metric_totals", fake_totals)
    monkeypatch.setattr("modules.metrics.stats.summarise_rollups", fake_summary)

    breakdown = asyncio.run(compute_latency_breakdown())
    overall = breakdown["overall"]
    assert overall["average_latency_ms"] == pytest.approx(200.0)
    assert overall["average_latency_per_frame_ms"] == pytest.approx(4.0)
    accel = overall["acceleration"]
    assert accel["accelerated"]["average_latency_ms"] == pytest.approx(200.0)
    assert accel["non_accelerated"]["average_latency_ms"] == pytest.approx(200.0)


def test_latency_stats_handles_mixed_inputs():
    stats = LatencyStats.from_payload(
        label="mixed",
        payload={
            "scans": "5",
            "duration_total_ms": "1000",
            "frames_total_scanned": "500",
            "frame_coverage_rate": "0.95",
        },
    )
    assert stats.average_latency_ms == pytest.approx(200.0)
    assert stats.average_latency_per_frame_ms == pytest.approx(2.0)
    assert stats.frame_coverage_rate == pytest.approx(0.95)

    stats_zero = LatencyStats.from_payload(
        label="zeros",
        payload={
            "scans": 0,
            "total_duration_ms": -50,
            "total_frames_scanned": None,
        },
    )
    assert stats_zero.average_latency_ms is None
    assert stats_zero.average_latency_per_frame_ms is None
