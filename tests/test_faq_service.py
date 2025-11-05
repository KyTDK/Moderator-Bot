import asyncio
import base64
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

os.environ.setdefault("FERNET_SECRET_KEY", base64.urlsafe_b64encode(b"0" * 32).decode())

@pytest.fixture
def anyio_backend():
    return "asyncio"

from modules.config.premium_plans import PLAN_FREE
from modules.faq.models import FAQEntry
from modules.faq import service
from modules.faq.service import backfill, operations, search

service.configure_developer_logging(bot=None)


@pytest.mark.anyio("asyncio")
async def test_add_entry_enforces_plan_limit(monkeypatch):
    async def fake_resolve_plan(_guild_id: int) -> str:
        return PLAN_FREE

    async def fake_count(_guild_id: int) -> int:
        return 5

    monkeypatch.setattr(operations, "_resolve_plan", fake_resolve_plan)
    monkeypatch.setattr(operations.storage, "count_entries", fake_count)

    with pytest.raises(service.FAQLimitError):
        await service.add_faq_entry(1234, "Question?", "Answer.")


@pytest.mark.anyio("asyncio")
async def test_add_entry_updates_vector_id(monkeypatch):
    async def fake_resolve_plan(_guild_id: int) -> str:
        return PLAN_FREE

    async def fake_count(_guild_id: int) -> int:
        return 0

    async def fake_insert(guild_id: int, question: str, answer: str) -> FAQEntry:
        return FAQEntry(guild_id=guild_id, entry_id=1, question=question, answer=answer, vector_id=None)

    async def fake_update_vector_id(guild_id: int, entry_id: int, vector_id: int) -> bool:
        return True

    async def fake_add_entry_vector(entry: FAQEntry) -> int:
        assert entry.entry_id == 1
        return 987

    monkeypatch.setattr(operations, "_resolve_plan", fake_resolve_plan)
    monkeypatch.setattr(operations.storage, "count_entries", fake_count)
    monkeypatch.setattr(operations.storage, "insert_entry", fake_insert)
    monkeypatch.setattr(operations.storage, "update_vector_id", fake_update_vector_id)
    monkeypatch.setattr(operations.vector_store, "is_available", lambda: True)
    monkeypatch.setattr(operations.vector_store, "add_entry", fake_add_entry_vector)

    entry = await service.add_faq_entry(42, "How to test?", "With pytest.")
    assert entry.vector_id == 987


@pytest.mark.anyio("asyncio")
async def test_add_entry_backfills_when_vectors_initially_unavailable(monkeypatch):
    # Ensure clean backfill state before the test.
    service._pending_vector_backfill.clear()
    if service._vector_backfill_task and not service._vector_backfill_task.done():
        service._vector_backfill_task.cancel()
        try:
            await service._vector_backfill_task
        except asyncio.CancelledError:
            pass
    service._vector_backfill_task = None
    service._backfill_attempts.clear()
    backfill._developer_log_last_unavailable = 0.0

    updates: list[tuple[int, int, int]] = []
    entries: dict[tuple[int, int], FAQEntry] = {}

    async def fake_resolve_plan(_guild_id: int) -> str:
        return PLAN_FREE

    async def fake_count(_guild_id: int) -> int:
        return 0

    async def fake_insert(guild_id: int, question: str, answer: str) -> FAQEntry:
        entry = FAQEntry(guild_id=guild_id, entry_id=1, question=question, answer=answer, vector_id=None)
        entries[(guild_id, entry.entry_id)] = entry
        return entry

    async def fake_fetch_entry(guild_id: int, entry_id: int) -> FAQEntry | None:
        return entries.get((guild_id, entry_id))

    async def fake_update_vector_id(guild_id: int, entry_id: int, vector_id: int) -> bool:
        updates.append((guild_id, entry_id, vector_id))
        stored = entries.get((guild_id, entry_id))
        if stored is not None:
            stored.vector_id = vector_id
        return True

    class VectorStub:
        def __init__(self) -> None:
            self.available = False
            self.calls = 0

        def is_available(self) -> bool:
            return self.available

        async def add_entry(self, _entry: FAQEntry) -> int | None:
            self.calls += 1
            if self.available:
                return 555
            return None

    stub = VectorStub()

    monkeypatch.setattr(operations, "_resolve_plan", fake_resolve_plan)
    monkeypatch.setattr(operations.storage, "count_entries", fake_count)
    monkeypatch.setattr(operations.storage, "insert_entry", fake_insert)
    monkeypatch.setattr(operations.storage, "fetch_entry", fake_fetch_entry)
    monkeypatch.setattr(operations.storage, "update_vector_id", fake_update_vector_id)
    monkeypatch.setattr(operations.vector_store, "is_available", stub.is_available)
    monkeypatch.setattr(operations.vector_store, "add_entry", stub.add_entry)
    monkeypatch.setattr(service, "_VECTOR_BACKFILL_RETRY_DELAY", 0.0)

    entry = await service.add_faq_entry(77, "a", "a")
    assert entry.vector_id is None
    assert stub.calls == 0
    assert updates == []
    assert (77, entry.entry_id) in service._pending_vector_backfill

    stub.available = True
    # Allow background backfill to run.
    for _ in range(10):
        await asyncio.sleep(0)

    assert updates == [(77, 1, 555)]
    assert entries[(77, 1)].vector_id == 555
    assert stub.calls >= 1
    assert (77, 1) not in service._pending_vector_backfill

    # Ensure background task has finished to avoid leaking state across tests.
    if service._vector_backfill_task and not service._vector_backfill_task.done():
        await service._vector_backfill_task
    service._pending_vector_backfill.clear()
    service._vector_backfill_task = None


@pytest.mark.anyio("asyncio")
async def test_find_best_faq_answer(monkeypatch):
    async def fake_fetch_entry(_guild_id: int, _entry_id: int) -> FAQEntry:
        return FAQEntry(guild_id=_guild_id, entry_id=1, question="How to reset?", answer="Use /faq.")

    def fake_query_chunks(chunks, *, guild_id, threshold, k):  # noqa: ARG001
        return [[{"entry_id": 1, "similarity": 0.85}] for _ in chunks]

    monkeypatch.setattr(search.vector_store, "is_available", lambda: True)
    monkeypatch.setattr(search.vector_store, "query_chunks", fake_query_chunks)
    monkeypatch.setattr(search.storage, "fetch_entry", fake_fetch_entry)

    result = await service.find_best_faq_answer(99, "hello how do I reset things", threshold=0.7)
    assert result is not None
    assert result.entry.entry_id == 1
    assert result.similarity == pytest.approx(0.85)
    assert result.used_fallback is False


@pytest.mark.anyio("asyncio")
async def test_find_best_faq_answer_fallback(monkeypatch):
    monkeypatch.setattr(search.vector_store, "is_available", lambda: False)

    async def fake_fetch_entries(guild_id: int) -> list[FAQEntry]:
        return [
            FAQEntry(
                guild_id=guild_id,
                entry_id=5,
                question="How do I link my account?",
                answer="Use /link-account.",
            )
        ]

    monkeypatch.setattr(search.storage, "fetch_entries", fake_fetch_entries)

    result = await service.find_best_faq_answer(
        123,
        "hey how do I link my account please",
        threshold=0.65,
    )
    assert result is not None
    assert result.entry.entry_id == 5
    assert result.similarity >= 0.65
    assert result.used_fallback is True
