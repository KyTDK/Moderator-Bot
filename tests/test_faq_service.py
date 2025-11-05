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


@pytest.mark.anyio("asyncio")
async def test_add_entry_enforces_plan_limit(monkeypatch):
    async def fake_resolve_plan(_guild_id: int) -> str:
        return PLAN_FREE

    async def fake_count(_guild_id: int) -> int:
        return 5

    monkeypatch.setattr(service, "_resolve_plan", fake_resolve_plan)
    monkeypatch.setattr(service.storage, "count_entries", fake_count)

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

    monkeypatch.setattr(service, "_resolve_plan", fake_resolve_plan)
    monkeypatch.setattr(service.storage, "count_entries", fake_count)
    monkeypatch.setattr(service.storage, "insert_entry", fake_insert)
    monkeypatch.setattr(service.storage, "update_vector_id", fake_update_vector_id)
    monkeypatch.setattr(service.vector_store, "is_available", lambda: True)
    monkeypatch.setattr(service.vector_store, "add_entry", fake_add_entry_vector)

    entry = await service.add_faq_entry(42, "How to test?", "With pytest.")
    assert entry.vector_id == 987


@pytest.mark.anyio("asyncio")
async def test_find_best_faq_answer(monkeypatch):
    async def fake_fetch_entry(_guild_id: int, _entry_id: int) -> FAQEntry:
        return FAQEntry(guild_id=_guild_id, entry_id=1, question="How to reset?", answer="Use /faq.")

    def fake_query_chunks(chunks, *, guild_id, threshold, k):  # noqa: ARG001
        return [[{"entry_id": 1, "similarity": 0.85}] for _ in chunks]

    monkeypatch.setattr(service.vector_store, "is_available", lambda: True)
    monkeypatch.setattr(service.vector_store, "query_chunks", fake_query_chunks)
    monkeypatch.setattr(service.storage, "fetch_entry", fake_fetch_entry)

    result = await service.find_best_faq_answer(99, "hello how do I reset things", threshold=0.7)
    assert result is not None
    assert result.entry.entry_id == 1
    assert result.similarity == pytest.approx(0.85)
