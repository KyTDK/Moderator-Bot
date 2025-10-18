from __future__ import annotations

import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))

from modules.metrics import (  # noqa: E402
    get_media_metric_global_rollups,
    get_media_metric_rollups,
    get_media_metrics_summary,
    get_media_metrics_totals,
    log_media_scan,
)
from modules.metrics import backend  # noqa: E402
from modules.metrics.backend.keys import totals_key as backend_totals_key  # noqa: E402


@dataclass
class _FakeConnectionPool:
    async def disconnect(self) -> None:  # pragma: no cover - interface shim
        return None


class FakeRedis:
    """Minimal Redis stand-in used by the metrics tests."""

    def __init__(self) -> None:
        self.streams: Dict[str, List[tuple[str, Dict[str, Any]]]] = defaultdict(list)
        self.hashes: Dict[str, Dict[str, str]] = defaultdict(dict)
        self.zsets: Dict[str, Dict[str, float]] = defaultdict(dict)
        self._stream_counter = 0
        self.connection_pool = _FakeConnectionPool()

    async def xadd(self, name: str, fields: Dict[str, Any], **kwargs: Any) -> str:
        self._stream_counter += 1
        entry_id = f"{self._stream_counter}-0"
        self.streams[name].append((entry_id, fields))
        maxlen = kwargs.get("maxlen")
        if isinstance(maxlen, int) and maxlen > 0:
            overflow = len(self.streams[name]) - maxlen
            if overflow > 0:
                self.streams[name] = self.streams[name][overflow:]
        return entry_id

    async def hincrby(self, name: str, field: str, amount: int) -> int:
        store = self.hashes.setdefault(name, {})
        current = int(store.get(field, 0) or 0)
        next_value = current + int(amount)
        store[field] = str(next_value)
        return next_value

    async def hset(self, name: str, mapping: Dict[str, Any]) -> int:
        store = self.hashes.setdefault(name, {})
        for key, value in mapping.items():
            if isinstance(value, (int, float)):
                store[key] = str(value)
            elif value is None:
                store[key] = ""
            else:
                store[key] = str(value)
        return len(mapping)

    async def hgetall(self, name: str) -> Dict[str, str]:
        store = self.hashes.get(name, {})
        return dict(store)

    async def zadd(self, name: str, mapping: Dict[str, float]) -> int:
        zset = self.zsets.setdefault(name, {})
        for member, score in mapping.items():
            zset[member] = float(score)
        return len(mapping)

    async def zrevrangebyscore(
        self,
        name: str,
        max_score: float | str,
        min_score: float | str,
        *,
        start: int | None = None,
        num: int | None = None,
    ) -> List[str]:
        zset = self.zsets.get(name, {})
        if not zset:
            return []
        max_value = self._convert_score(max_score)
        min_value = self._convert_score(min_score)
        filtered = [
            (member, score)
            for member, score in zset.items()
            if score <= max_value and score >= min_value
        ]
        filtered.sort(key=lambda item: (item[1], item[0]), reverse=True)
        members = [member for member, _ in filtered]
        if start is not None or num is not None:
            start_idx = start or 0
            end_idx = start_idx + (num or len(members))
            members = members[start_idx:end_idx]
        return members

    async def delete(self, name: str) -> int:
        removed = 0
        if name in self.hashes:
            self.hashes.pop(name, None)
            removed += 1
        if name in self.zsets:
            self.zsets.pop(name, None)
            removed += 1
        if name in self.streams:
            self.streams.pop(name, None)
            removed += 1
        return removed

    async def close(self) -> None:  # pragma: no cover - interface shim
        return None

    @staticmethod
    def _convert_score(value: float | str) -> float:
        if isinstance(value, (int, float)):
            return float(value)
        text = value.lower()
        if text in {"+inf", "inf"}:
            return float("inf")
        if text == "-inf":
            return float("-inf")
        return float(text)


@pytest.fixture(autouse=True)
def _patch_metrics_backend(monkeypatch: pytest.MonkeyPatch) -> Iterable[FakeRedis]:
    client = FakeRedis()
    backend.set_client_override(client)
    yield client
    backend.set_client_override(None)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


RESET_RAW_EXACT = {
    "total_duration_ms",
    "total_duration_sq_ms",
    "total_bytes",
    "total_bytes_sq",
    "total_frames_scanned",
    "total_frames_target",
}

RESET_RAW_SUFFIXES = (
    "_total_duration_ms",
    "_total_duration_sq_ms",
    "_total_bytes",
    "_total_bytes_sq",
    "_total_frames_scanned",
    "_total_frames_target",
    "duration_total_ms",
    "duration_total_sq_ms",
    "bytes_total",
    "bytes_total_sq",
    "frames_total_scanned",
    "frames_total_target",
)


def _zero_raw_totals(store: dict[str, str]) -> None:
    for field in list(store.keys()):
        if field in RESET_RAW_EXACT or any(field.endswith(suffix) for suffix in RESET_RAW_SUFFIXES):
            store[field] = "0"


@pytest.mark.anyio("asyncio")
async def test_media_scan_metrics_flow(_patch_metrics_backend: FakeRedis) -> None:
    client = _patch_metrics_backend
    base_time = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)

    await log_media_scan(
        guild_id=123,
        channel_id=10,
        user_id=42,
        message_id=555,
        content_type="image",
        detected_mime="image/png",
        filename="flagged.png",
        file_size=1500,
        source="attachment",
        scan_result={
            "is_nsfw": True,
            "reason": "openai_moderation",
            "summary_categories": {"adult": 0.9, "violence": 0.65},
            "score": 0.97,
        },
        scan_duration_ms=120,
        accelerated=True,
        reference="flagged_ref",
        occurred_at=base_time,
    )

    await log_media_scan(
        guild_id=123,
        channel_id=10,
        user_id=99,
        message_id=556,
        content_type="image",
        detected_mime="image/png",
        filename="safe.png",
        file_size=1500,
        source="attachment",
        scan_result={
            "is_nsfw": False,
            "reason": "openai_moderation",
            "summary_categories": {"adult": 0.2},
            "score": 0.11,
        },
        scan_duration_ms=80,
        accelerated=True,
        reference="safe_ref",
        occurred_at=base_time + timedelta(hours=1),
    )

    await log_media_scan(
        guild_id=123,
        channel_id=11,
        user_id=77,
        message_id=600,
        content_type="video",
        detected_mime="video/mp4",
        filename="clip.mp4",
        file_size=500,
        source="attachment",
        scan_result={
            "video_frames_scanned": 15,
            "video_frames_target": 30,
        },
        status="unsupported_type",
        scan_duration_ms=60,
        accelerated=False,
        reference="error_ref",
        extra_context={"reason": "mime_unsupported"},
        occurred_at=base_time + timedelta(days=1),
    )

    rollups = await get_media_metric_rollups(guild_id=123, limit=10)
    assert len(rollups) == 2

    latest = rollups[0]
    assert latest["content_type"] == "video"
    assert latest["scans_count"] == 1
    assert latest["flagged_count"] == 0
    assert latest["status_counts"]["unsupported_type"] == 1
    assert latest["last_status"] == "unsupported_type"
    assert latest["last_latency_ms"] == 60
    assert latest["average_latency_ms"] == pytest.approx(60.0)
    assert latest["latency_std_dev_ms"] == pytest.approx(0.0)
    assert latest["total_duration_sq_ms"] == 60 * 60
    assert latest["average_bytes"] == pytest.approx(500.0)
    assert latest["bytes_std_dev"] == pytest.approx(0.0)
    assert latest["flagged_rate"] == pytest.approx(0.0)
    assert latest["average_flags_per_scan"] == pytest.approx(0.0)
    assert latest["last_details"]["context"]["reason"] == "mime_unsupported"
    assert latest["total_frames_scanned"] == 15
    assert latest["total_frames_target"] == 30
    assert latest["average_frames_per_scan"] == pytest.approx(15.0)
    assert latest["average_latency_per_frame_ms"] == pytest.approx(4.0)
    assert latest["frames_per_second"] == pytest.approx(250.0)
    assert latest["frame_coverage_rate"] == pytest.approx(0.5)
    assert latest["acceleration"]["non_accelerated"]["scans_count"] == 1
    assert latest["acceleration"]["non_accelerated"]["average_latency_ms"] == pytest.approx(60.0)
    assert latest["acceleration"]["non_accelerated"]["total_frames_scanned"] == 15
    assert latest["acceleration"]["non_accelerated"]["average_frames_per_scan"] == pytest.approx(15.0)
    assert latest["acceleration"]["non_accelerated"]["average_latency_per_frame_ms"] == pytest.approx(4.0)
    assert latest["acceleration"]["non_accelerated"]["frames_per_second"] == pytest.approx(250.0)
    assert latest["acceleration"]["non_accelerated"]["frame_coverage_rate"] == pytest.approx(0.5)
    assert latest["acceleration"]["accelerated"]["scans_count"] == 0
    assert latest["acceleration"]["unknown"]["scans_count"] == 0
    assert latest["updated_at"] is not None

    day_one = rollups[1]
    assert day_one["content_type"] == "image"
    assert day_one["scans_count"] == 2
    assert day_one["flagged_count"] == 1
    assert day_one["flags_sum"] == 2
    assert day_one["total_bytes"] == 3000
    assert day_one["total_duration_ms"] == 200
    assert day_one["last_latency_ms"] == 80
    assert day_one["average_latency_ms"] == pytest.approx(100.0)
    assert day_one["latency_std_dev_ms"] == pytest.approx(20.0)
    assert day_one["total_duration_sq_ms"] == 20800
    assert day_one["average_bytes"] == pytest.approx(1500.0)
    assert day_one["bytes_std_dev"] == pytest.approx(0.0)
    assert day_one["flagged_rate"] == pytest.approx(0.5)
    assert day_one["average_flags_per_scan"] == pytest.approx(1.0)
    assert day_one["status_counts"]["scan_complete"] == 2
    assert day_one["last_reference"] == "flagged_ref"
    assert day_one["last_flagged_at"] == base_time
    assert day_one["updated_at"] is not None
    accel_day_one = day_one["acceleration"]["accelerated"]
    assert accel_day_one["scans_count"] == 2
    assert accel_day_one["flagged_count"] == 1
    assert accel_day_one["total_duration_ms"] == 200
    assert accel_day_one["total_duration_sq_ms"] == 20800
    assert accel_day_one["average_latency_ms"] == pytest.approx(100.0)
    assert accel_day_one["latency_std_dev_ms"] == pytest.approx(20.0)
    assert accel_day_one["flagged_rate"] == pytest.approx(0.5)
    assert accel_day_one["average_flags_per_scan"] == pytest.approx(1.0)
    assert accel_day_one["last_reference"] == "safe_ref"
    assert day_one["acceleration"]["non_accelerated"]["scans_count"] == 0
    assert day_one["acceleration"]["unknown"]["scans_count"] == 0

    summary = await get_media_metrics_summary(guild_id=123)
    summary_map = {entry["content_type"]: entry for entry in summary}
    assert summary_map["image"]["scans"] == 2
    assert summary_map["image"]["flagged"] == 1
    assert summary_map["image"]["flags_sum"] == 2
    assert summary_map["image"]["bytes_total"] == 3000
    assert summary_map["image"]["average_latency_ms"] == pytest.approx(100.0)
    assert summary_map["image"]["latency_std_dev_ms"] == pytest.approx(20.0)
    assert summary_map["image"]["flagged_rate"] == pytest.approx(0.5)
    assert summary_map["image"]["average_flags_per_scan"] == pytest.approx(1.0)
    assert summary_map["image"]["acceleration"]["accelerated"]["scans"] == 2
    assert summary_map["image"]["acceleration"]["accelerated"]["average_latency_ms"] == pytest.approx(100.0)
    assert summary_map["image"]["acceleration"]["non_accelerated"]["scans"] == 0
    assert summary_map["video"]["scans"] == 1
    assert summary_map["video"]["flagged"] == 0
    assert summary_map["video"]["bytes_total"] == 500
    assert summary_map["video"]["average_latency_ms"] == pytest.approx(60.0)
    assert summary_map["video"]["frames_total_scanned"] == 15
    assert summary_map["video"]["frames_total_target"] == 30
    assert summary_map["video"]["average_frames_per_scan"] == pytest.approx(15.0)
    assert summary_map["video"]["average_latency_per_frame_ms"] == pytest.approx(4.0)
    assert summary_map["video"]["frames_per_second"] == pytest.approx(250.0)
    assert summary_map["video"]["frame_coverage_rate"] == pytest.approx(0.5)
    assert summary_map["video"]["acceleration"]["non_accelerated"]["scans"] == 1
    assert summary_map["video"]["acceleration"]["non_accelerated"]["frames_total_scanned"] == 15
    assert summary_map["video"]["acceleration"]["non_accelerated"]["average_frames_per_scan"] == pytest.approx(15.0)
    assert summary_map["video"]["acceleration"]["non_accelerated"]["average_latency_per_frame_ms"] == pytest.approx(4.0)
    assert summary_map["video"]["acceleration"]["non_accelerated"]["frames_per_second"] == pytest.approx(250.0)
    assert summary_map["video"]["acceleration"]["non_accelerated"]["frame_coverage_rate"] == pytest.approx(0.5)

    global_summary = await get_media_metrics_summary()
    global_summary_map = {entry["content_type"]: entry for entry in global_summary}
    assert global_summary_map["image"]["scans"] == 2
    assert global_summary_map["video"]["scans"] == 1

    totals = await get_media_metrics_totals()
    assert totals["scans_count"] == 3
    assert totals["flagged_count"] == 1
    assert totals["flags_sum"] == 2
    assert totals["total_bytes"] == 3500
    assert totals["total_duration_ms"] == 260
    assert totals["total_frames_scanned"] == 15
    assert totals["total_frames_target"] == 30
    assert totals["last_latency_ms"] == 60
    assert totals["average_latency_ms"] == pytest.approx(86.6666666667)
    assert totals["latency_std_dev_ms"] == pytest.approx(24.958, rel=1e-3)
    assert totals["total_duration_sq_ms"] == 24400
    assert totals["average_bytes"] == pytest.approx(1166.6666666667)
    assert totals["bytes_std_dev"] == pytest.approx(471.4045, rel=1e-4)
    assert totals["flagged_rate"] == pytest.approx(1 / 3)
    assert totals["average_flags_per_scan"] == pytest.approx(2 / 3)
    assert totals["average_frames_per_scan"] == pytest.approx(5.0)
    assert totals["average_latency_per_frame_ms"] == pytest.approx(17.3333333333)
    assert totals["frames_per_second"] == pytest.approx(57.6923076923)
    assert totals["frame_coverage_rate"] == pytest.approx(0.5)
    assert totals["status_counts"]["scan_complete"] == 2
    assert totals["status_counts"]["unsupported_type"] == 1
    assert totals["last_status"] == "unsupported_type"
    assert totals["last_reference"] == "flagged_ref"
    assert totals["last_flagged_at"] == base_time
    assert totals["last_details"]["file"]["name"] == "clip.mp4"
    assert totals["updated_at"] is not None
    accel_totals = totals["acceleration"]["accelerated"]
    assert accel_totals["scans_count"] == 2
    assert accel_totals["flagged_count"] == 1
    assert accel_totals["total_duration_ms"] == 200
    assert accel_totals["latency_std_dev_ms"] == pytest.approx(20.0)
    assert accel_totals["total_frames_scanned"] == 0
    assert accel_totals["average_latency_per_frame_ms"] == pytest.approx(100.0)
    assert totals["acceleration"]["non_accelerated"]["scans_count"] == 1
    assert totals["acceleration"]["non_accelerated"]["total_frames_scanned"] == 15
    assert totals["acceleration"]["non_accelerated"]["average_latency_per_frame_ms"] == pytest.approx(4.0)
    assert totals["acceleration"]["non_accelerated"]["frames_per_second"] == pytest.approx(250.0)
    assert totals["acceleration"]["non_accelerated"]["frame_coverage_rate"] == pytest.approx(0.5)
    assert totals["acceleration"]["unknown"]["scans_count"] == 0

    global_rollups = await get_media_metric_global_rollups(limit=10)
    assert len(global_rollups) == 2

    global_latest = global_rollups[0]
    assert global_latest["content_type"] == "video"
    assert global_latest["scans_count"] == 1
    assert global_latest["total_duration_ms"] == 60
    assert global_latest["acceleration"]["non_accelerated"]["scans_count"] == 1

    global_day_one = global_rollups[1]
    assert global_day_one["content_type"] == "image"
    assert global_day_one["scans_count"] == 2
    assert global_day_one["flagged_count"] == 1
    assert global_day_one["acceleration"]["accelerated"]["flagged_rate"] == pytest.approx(0.5)

    recent = await get_media_metric_rollups(
        guild_id=123,
        since=(base_time + timedelta(days=1)).date(),
        limit=10,
    )
    assert len(recent) == 1
    assert recent[0]["content_type"] == "video"

    # Ensure the Redis stream captured each event.
    stream_entries = client.streams["moderator:metrics"]
    assert len(stream_entries) == 3
    payloads = [entry[1]["event"] for entry in stream_entries]
    accelerations = set()
    for raw in payloads:
        decoded = json.loads(raw)
        assert decoded["content_type"] in {"image", "video"}
        accelerations.add(decoded.get("accelerated"))
    assert accelerations == {True, False}


@pytest.mark.anyio("asyncio")
async def test_resetting_raw_totals_clears_global_metrics(_patch_metrics_backend: FakeRedis) -> None:
    base_time = datetime(2024, 2, 1, 9, 0, tzinfo=timezone.utc)

    await log_media_scan(
        guild_id=321,
        channel_id=20,
        user_id=84,
        message_id=777,
        content_type="image",
        detected_mime="image/png",
        filename="example.png",
        file_size=2048,
        source="attachment",
        scan_result={"is_nsfw": True},
        scan_duration_ms=150,
        accelerated=True,
        occurred_at=base_time,
    )

    totals_before = await get_media_metrics_totals()
    assert totals_before["average_latency_ms"] > 0.0

    totals_store = _patch_metrics_backend.hashes[backend_totals_key()]
    _zero_raw_totals(totals_store)

    totals_after = await get_media_metrics_totals()
    assert totals_after["average_latency_ms"] == pytest.approx(0.0)
    assert totals_after["average_bytes"] == pytest.approx(0.0)
    assert totals_after["average_latency_per_frame_ms"] == pytest.approx(0.0)
    assert totals_after["frames_per_second"] == pytest.approx(0.0)


@pytest.mark.anyio("asyncio")
async def test_resetting_raw_totals_clears_rollup_metrics(_patch_metrics_backend: FakeRedis) -> None:
    base_time = datetime(2024, 2, 1, 9, 0, tzinfo=timezone.utc)

    await log_media_scan(
        guild_id=555,
        channel_id=33,
        user_id=999,
        message_id=888,
        content_type="video",
        detected_mime="video/mp4",
        filename="segment.mp4",
        file_size=1000,
        source="attachment",
        scan_result={
            "video_frames_scanned": 12,
            "video_frames_target": 24,
        },
        scan_duration_ms=300,
        accelerated=False,
        occurred_at=base_time,
    )

    summary_before = await get_media_metrics_summary(guild_id=555)
    assert summary_before
    video_summary_before = next(entry for entry in summary_before if entry["content_type"] == "video")
    assert video_summary_before["average_latency_ms"] > 0.0

    for key, store in list(_patch_metrics_backend.hashes.items()):
        if ":rollup:" in key and not key.endswith(":status"):
            _zero_raw_totals(store)

    summary_after = await get_media_metrics_summary(guild_id=555)
    video_summary_after = next(entry for entry in summary_after if entry["content_type"] == "video")
    assert video_summary_after["average_latency_ms"] == pytest.approx(0.0)
    assert video_summary_after["average_frames_per_scan"] == pytest.approx(0.0)
    assert video_summary_after["frames_per_second"] == pytest.approx(0.0)
    assert video_summary_after["average_latency_per_frame_ms"] == pytest.approx(0.0)
