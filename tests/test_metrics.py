from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import types
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("FERNET_SECRET_KEY", "DeJ3sXDDTTbikeRSJzRgg8r_Ch61_NbE8D3LWnLOJO4=")

if "cryptography" not in sys.modules:
    cryptography_stub = types.ModuleType("cryptography")
    fernet_stub = types.ModuleType("cryptography.fernet")

    class _DummyFernet:
        def __init__(self, key: bytes) -> None:
            self.key = key

        def encrypt(self, data: bytes) -> bytes:
            return data

        def decrypt(self, token: bytes) -> bytes:
            return token

    fernet_stub.Fernet = _DummyFernet
    cryptography_stub.fernet = fernet_stub
    sys.modules["cryptography"] = cryptography_stub
    sys.modules["cryptography.fernet"] = fernet_stub

if "dotenv" not in sys.modules:
    dotenv_stub = types.ModuleType("dotenv")

    def _load_dotenv(*args: Any, **kwargs: Any) -> None:
        return None

    dotenv_stub.load_dotenv = _load_dotenv
    sys.modules["dotenv"] = dotenv_stub

if "aiomysql" not in sys.modules:
    aiomysql_stub = types.ModuleType("aiomysql")

    class _DictCursor:
        ...

    class _FakePool:
        ...

    class _FakeConnection:
        ...

    async def _unsupported(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("aiomysql operations should be stubbed in tests")

    aiomysql_stub.DictCursor = _DictCursor
    aiomysql_stub.Pool = _FakePool
    aiomysql_stub.Connection = _FakeConnection
    aiomysql_stub.create_pool = _unsupported
    aiomysql_stub.connect = _unsupported
    sys.modules["aiomysql"] = aiomysql_stub

if "modules.config.settings_schema" not in sys.modules:
    settings_schema_stub = types.ModuleType("modules.config.settings_schema")
    settings_schema_stub.SETTINGS_SCHEMA = {}
    sys.modules["modules.config.settings_schema"] = settings_schema_stub
    modules_config = importlib.import_module("modules.config")
    setattr(modules_config, "settings_schema", settings_schema_stub)

from modules.metrics import (
    get_media_metric_rollups,
    get_media_metrics_summary,
    get_media_metrics_totals,
    log_media_scan,
)


class FakeCursor:
    def __init__(self, store: "MetricsDataStore") -> None:
        self.store = store
        self._fetchone_result: Any = None
        self._fetchall_result: list[Any] = []
        self.rowcount: int = 0

    async def __aenter__(self) -> "FakeCursor":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    async def execute(self, query: str, params: Iterable[Any] = ()) -> None:
        normalized = " ".join(query.split())
        params = tuple(params)

        if "FROM moderation_metric_rollups" in normalized and "FOR UPDATE" in normalized:
            metric_date, guild_id, content_type = params
            key = (metric_date, int(guild_id), content_type)
            row = self.store.rollups.get(key)
            if row:
                self._fetchone_result = (
                    row["scans_count"],
                    row["flagged_count"],
                    row["flags_sum"],
                    row["total_bytes"],
                    row["total_duration_ms"],
                    row["last_duration_ms"],
                    row["last_flagged_at"],
                    row["last_reference"],
                    row["last_status"],
                    json.dumps(row["status_counts"], ensure_ascii=False),
                    row["last_details"],
                )
                self.rowcount = 1
            else:
                self._fetchone_result = None
                self.rowcount = 0
            self._fetchall_result = []
            return

        if normalized.startswith("UPDATE moderation_metric_rollups"):
            (
                scans_count,
                flagged_count,
                flags_sum,
                total_bytes,
                total_duration_ms,
                last_duration_ms,
                last_flagged_at,
                last_reference,
                last_status,
                status_counts_json,
                last_details,
                metric_date,
                guild_id,
                content_type,
            ) = params
            key = (metric_date, int(guild_id), content_type)
            row = self.store.rollups.setdefault(
                key,
                {
                    "scans_count": 0,
                    "flagged_count": 0,
                    "flags_sum": 0,
                    "total_bytes": 0,
                    "total_duration_ms": 0,
                    "last_duration_ms": 0,
                    "status_counts": {},
                    "last_flagged_at": None,
                    "last_reference": None,
                    "last_status": None,
                    "last_details": None,
                },
            )
            row.update(
                {
                    "scans_count": int(scans_count),
                    "flagged_count": int(flagged_count),
                    "flags_sum": int(flags_sum),
                    "total_bytes": int(total_bytes),
                    "total_duration_ms": int(total_duration_ms),
                    "last_duration_ms": int(last_duration_ms),
                    "last_flagged_at": last_flagged_at,
                    "last_reference": last_reference,
                    "last_status": last_status,
                    "status_counts": json.loads(status_counts_json),
                    "last_details": last_details,
                }
            )
            self.rowcount = 1
            self._fetchone_result = None
            self._fetchall_result = []
            return

        if normalized.startswith("INSERT INTO moderation_metric_rollups"):
            (
                metric_date,
                guild_id,
                content_type,
                scans_count,
                flagged_count,
                flags_sum,
                total_bytes,
                total_duration_ms,
                last_duration_ms,
                last_flagged_at,
                last_reference,
                last_status,
                status_counts_json,
                last_details,
            ) = params
            self.store.rollups[(metric_date, int(guild_id), content_type)] = {
                "scans_count": int(scans_count),
                "flagged_count": int(flagged_count),
                "flags_sum": int(flags_sum),
                "total_bytes": int(total_bytes),
                "total_duration_ms": int(total_duration_ms),
                "last_duration_ms": int(last_duration_ms),
                "status_counts": json.loads(status_counts_json),
                "last_flagged_at": last_flagged_at,
                "last_reference": last_reference,
                "last_status": last_status,
                "last_details": last_details,
            }
            self.rowcount = 1
            self._fetchone_result = None
            self._fetchall_result = []
            return

        if "FROM moderation_metric_totals" in normalized and "FOR UPDATE" in normalized:
            row = self.store.totals
            if row:
                self._fetchone_result = (
                    row["scans_count"],
                    row["flagged_count"],
                    row["flags_sum"],
                    row["total_bytes"],
                    row["total_duration_ms"],
                    row["last_duration_ms"],
                    row["last_flagged_at"],
                    row["last_reference"],
                    row["last_status"],
                    json.dumps(row["status_counts"], ensure_ascii=False),
                    row["last_details"],
                )
                self.rowcount = 1
            else:
                self._fetchone_result = None
                self.rowcount = 0
            self._fetchall_result = []
            return

        if normalized.startswith("UPDATE moderation_metric_totals"):
            (
                scans_count,
                flagged_count,
                flags_sum,
                total_bytes,
                total_duration_ms,
                last_duration_ms,
                last_flagged_at,
                last_reference,
                last_status,
                status_counts_json,
                last_details,
            ) = params
            row = self.store.totals or self.store.create_empty_totals()
            row.update(
                {
                    "scans_count": int(scans_count),
                    "flagged_count": int(flagged_count),
                    "flags_sum": int(flags_sum),
                    "total_bytes": int(total_bytes),
                    "total_duration_ms": int(total_duration_ms),
                    "last_duration_ms": int(last_duration_ms),
                    "last_flagged_at": last_flagged_at,
                    "last_reference": last_reference,
                    "last_status": last_status,
                    "status_counts": json.loads(status_counts_json),
                    "last_details": last_details,
                }
            )
            self.store.totals = row
            self.rowcount = 1
            self._fetchone_result = None
            self._fetchall_result = []
            return

        if normalized.startswith("INSERT INTO moderation_metric_totals"):
            (
                singleton_id,
                scans_count,
                flagged_count,
                flags_sum,
                total_bytes,
                total_duration_ms,
                last_duration_ms,
                last_flagged_at,
                last_reference,
                last_status,
                status_counts_json,
                last_details,
            ) = params
            assert int(singleton_id) == 1
            self.store.totals = {
                "scans_count": int(scans_count),
                "flagged_count": int(flagged_count),
                "flags_sum": int(flags_sum),
                "total_bytes": int(total_bytes),
                "total_duration_ms": int(total_duration_ms),
                "last_duration_ms": int(last_duration_ms),
                "last_flagged_at": last_flagged_at,
                "last_reference": last_reference,
                "last_status": last_status,
                "status_counts": json.loads(status_counts_json),
                "last_details": last_details,
            }
            self.rowcount = 1
            self._fetchone_result = None
            self._fetchall_result = []
            return

        raise AssertionError(f"Unhandled query: {normalized}")

    async def fetchone(self) -> Any:
        return self._fetchone_result

    async def fetchall(self) -> list[Any]:
        return self._fetchall_result


class FakeConnection:
    def __init__(self, store: "MetricsDataStore") -> None:
        self.store = store

    async def begin(self) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.store)


class FakeConnectionContext:
    def __init__(self, store: "MetricsDataStore") -> None:
        self._conn = FakeConnection(store)

    async def __aenter__(self) -> FakeConnection:
        return self._conn

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class FakePool:
    def __init__(self, store: "MetricsDataStore") -> None:
        self.store = store

    def acquire(self) -> FakeConnectionContext:
        return FakeConnectionContext(self.store)


class MetricsDataStore:
    def __init__(self) -> None:
        self.rollups: dict[tuple[date, int, str], dict[str, Any]] = {}
        self.totals: dict[str, Any] | None = None

    def create_empty_totals(self) -> dict[str, Any]:
        return {
            "scans_count": 0,
            "flagged_count": 0,
            "flags_sum": 0,
            "total_bytes": 0,
            "total_duration_ms": 0,
            "last_duration_ms": 0,
            "status_counts": {},
            "last_flagged_at": None,
            "last_status": None,
            "last_reference": None,
            "last_details": None,
        }

    async def get_pool(self) -> FakePool:
        return FakePool(self)

    async def execute_query(
        self,
        query: str,
        params: Iterable[Any] = (),
        *,
        commit: bool = True,
        fetch_one: bool = False,
        fetch_all: bool = False,
    ) -> tuple[Any, int]:
        normalized = " ".join(query.split())
        params = tuple(params)

        if "FROM moderation_metric_rollups" in normalized and "GROUP BY" not in normalized:
            result = self._select_rollups(normalized, params)
            return result, len(result)

        if "FROM moderation_metric_rollups" in normalized and "GROUP BY" in normalized:
            result = self._summarise_rollups(normalized, params)
            return result, len(result)

        if "FROM moderation_metric_totals" in normalized:
            row = self._select_totals()
            return row, 1 if row is not None else 0

        raise AssertionError(f"Unhandled execute_query call: {normalized}")

    def _select_rollups(self, normalized: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        if not params:
            limit = None
            filter_params: list[Any] = []
        else:
            limit = int(params[-1])
            filter_params = list(params[:-1])

        idx = 0
        guild_filter = None
        content_filter = None
        since_filter = None

        if "guild_id = %s" in normalized:
            guild_filter = int(filter_params[idx])
            idx += 1
        if "content_type = %s" in normalized:
            content_filter = filter_params[idx]
            idx += 1
        if "metric_date >= %s" in normalized:
            since_filter = filter_params[idx]

        rows = []
        for (metric_date, guild_id, content_type), data in self.rollups.items():
            if guild_filter is not None and guild_id != guild_filter:
                continue
            if content_filter is not None and content_type != content_filter:
                continue
            if since_filter is not None and metric_date < since_filter:
                continue
            rows.append((metric_date, guild_id, content_type, data))

        rows.sort(key=lambda item: item[0], reverse=True)

        if limit is not None:
            rows = rows[:limit]

        result = []
        for metric_date, guild_id, content_type, data in rows:
            result.append(
                (
                    metric_date,
                    guild_id,
                    content_type,
                    data["scans_count"],
                    data["flagged_count"],
                    data["flags_sum"],
                    data["total_bytes"],
                    data["total_duration_ms"],
                    data["last_duration_ms"],
                    data["last_flagged_at"],
                    data["last_reference"],
                    data["last_status"],
                    json.dumps(data["status_counts"], ensure_ascii=False),
                    data["last_details"],
                )
            )
        return result

    def _summarise_rollups(self, normalized: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        idx = 0
        guild_filter = None
        since_filter = None

        if "guild_id = %s" in normalized:
            guild_filter = int(params[idx])
            idx += 1
        if "metric_date >= %s" in normalized:
            since_filter = params[idx]

        aggregates: dict[str, dict[str, int]] = defaultdict(
            lambda: {
                "scans": 0,
                "flagged": 0,
                "flags_sum": 0,
                "bytes_total": 0,
                "duration_total": 0,
            }
        )
        for (metric_date, guild_id, content_type), data in self.rollups.items():
            if guild_filter is not None and guild_id != guild_filter:
                continue
            if since_filter is not None and metric_date < since_filter:
                continue
            entry = aggregates[content_type]
            entry["scans"] += int(data["scans_count"])
            entry["flagged"] += int(data["flagged_count"])
            entry["flags_sum"] += int(data["flags_sum"])
            entry["bytes_total"] += int(data["total_bytes"])
            entry["duration_total"] += int(data["total_duration_ms"])

        result = []
        for content_type, values in aggregates.items():
            result.append(
                (
                    content_type,
                    values["scans"],
                    values["flagged"],
                    values["flags_sum"],
                    values["bytes_total"],
                    values["duration_total"],
                )
            )

        result.sort(key=lambda item: item[1], reverse=True)
        return result

    def _select_totals(self) -> tuple[Any, ...] | None:
        row = self.totals
        if not row:
            return None
        return (
            row["scans_count"],
            row["flagged_count"],
            row["flags_sum"],
            row["total_bytes"],
            row["total_duration_ms"],
            row["last_duration_ms"],
            row["last_flagged_at"],
            row["last_reference"],
            row["last_status"],
            json.dumps(row["status_counts"], ensure_ascii=False),
            row["last_details"],
        )


@pytest.fixture
def metrics_store(monkeypatch: pytest.MonkeyPatch) -> MetricsDataStore:
    store = MetricsDataStore()

    async def fake_get_pool() -> FakePool:
        return await store.get_pool()

    async def fake_execute_query(
        query: str,
        params: Iterable[Any] = (),
        *,
        commit: bool = True,
        fetch_one: bool = False,
        fetch_all: bool = False,
    ) -> tuple[Any, int]:
        return await store.execute_query(
            query,
            params,
            commit=commit,
            fetch_one=fetch_one,
            fetch_all=fetch_all,
        )

    monkeypatch.setattr("modules.utils.mysql.metrics.rollups.get_pool", fake_get_pool)
    monkeypatch.setattr("modules.utils.mysql.metrics.rollups.execute_query", fake_execute_query)
    monkeypatch.setattr("modules.utils.mysql.metrics.totals.get_pool", fake_get_pool)
    monkeypatch.setattr("modules.utils.mysql.metrics.totals.execute_query", fake_execute_query)
    return store


def test_metrics_totals_and_rollups(metrics_store: MetricsDataStore) -> None:
    async def run() -> None:
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
            scan_result=None,
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
        assert latest["last_details"]["context"]["reason"] == "mime_unsupported"

        day_one = rollups[1]
        assert day_one["content_type"] == "image"
        assert day_one["scans_count"] == 2
        assert day_one["flagged_count"] == 1
        assert day_one["flags_sum"] == 2
        assert day_one["total_bytes"] == 3000
        assert day_one["total_duration_ms"] == 200
        assert day_one["last_latency_ms"] == 80
        assert day_one["average_latency_ms"] == pytest.approx(100.0)
        assert day_one["status_counts"]["scan_complete"] == 2
        assert day_one["last_reference"] == "flagged_ref"
        assert day_one["last_flagged_at"] == base_time

        summary = await get_media_metrics_summary(guild_id=123)
        summary_map = {entry["content_type"]: entry for entry in summary}

        assert summary_map["image"]["scans"] == 2
        assert summary_map["image"]["flagged"] == 1
        assert summary_map["image"]["flags_sum"] == 2
        assert summary_map["image"]["bytes_total"] == 3000

        assert summary_map["video"]["scans"] == 1
        assert summary_map["video"]["flagged"] == 0
        assert summary_map["video"]["bytes_total"] == 500

        totals = await get_media_metrics_totals()
        assert totals["scans_count"] == 3
        assert totals["flagged_count"] == 1
        assert totals["flags_sum"] == 2
        assert totals["total_bytes"] == 3500
        assert totals["total_duration_ms"] == 260
        assert totals["last_latency_ms"] == 60
        assert totals["average_latency_ms"] == pytest.approx(86.6666666667)
        assert totals["status_counts"]["scan_complete"] == 2
        assert totals["status_counts"]["unsupported_type"] == 1
        assert totals["last_status"] == "unsupported_type"
        assert totals["last_reference"] == "flagged_ref"
        assert totals["last_flagged_at"] == base_time
        assert totals["last_details"]["file"]["name"] == "clip.mp4"

        recent = await get_media_metric_rollups(
            guild_id=123,
            since=(base_time + timedelta(days=1)).date(),
            limit=10,
        )
        assert len(recent) == 1
        assert recent[0]["content_type"] == "video"

    asyncio.run(run())
