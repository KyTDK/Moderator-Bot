from __future__ import annotations

from typing import Any, Optional

from modules.config.premium_plans import (
    PLAN_CORE,
    PLAN_DISPLAY_NAMES,
    PLAN_FREE,
    PLAN_PRO,
    PLAN_ULTRA,
)
from modules.faq import storage, vector_store
from modules.faq.models import FAQEntry
from modules.utils import mysql

from .backfill import _queue_vector_backfill, _remove_from_backfill

__all__ = [
    "FAQServiceError",
    "FAQLimitError",
    "FAQEntryNotFoundError",
    "list_faq_entries",
    "add_faq_entry",
    "delete_faq_entry",
]

FAQ_LIMITS: dict[str, Optional[int]] = {
    PLAN_FREE: 5,
    PLAN_CORE: 20,
    PLAN_PRO: 100,
    PLAN_ULTRA: None,
}


class FAQServiceError(RuntimeError):
    """Base exception for FAQ service operations."""


class FAQLimitError(FAQServiceError):
    """Raised when a guild exceeds its FAQ allotment."""

    def __init__(self, limit: int, plan: str) -> None:
        display_plan = PLAN_DISPLAY_NAMES.get(plan, plan.title())
        super().__init__(f"Limit of {limit} FAQs reached for the {display_plan} plan")
        self.limit = limit
        self.plan = plan


class FAQEntryNotFoundError(FAQServiceError):
    """Raised when the requested FAQ entry does not exist."""

    def __init__(self, entry_id: int) -> None:
        super().__init__(f"FAQ entry {entry_id} not found")
        self.entry_id = entry_id


async def _resolve_plan(guild_id: int) -> str:
    plan = await mysql.resolve_guild_plan(guild_id)
    return plan or PLAN_FREE


def _limit_for_plan(plan: str) -> Optional[int]:
    return FAQ_LIMITS.get(plan, FAQ_LIMITS[PLAN_FREE])


async def list_faq_entries(guild_id: int) -> list[FAQEntry]:
    entries = await storage.fetch_entries(guild_id)
    if entries:
        for entry in entries:
            if entry.vector_id is None:
                _queue_vector_backfill(entry)
    return entries


async def add_faq_entry(guild_id: int, question: str, answer: str) -> FAQEntry:
    normalized_question = question.strip()
    normalized_answer = answer.strip()
    if not normalized_question or not normalized_answer:
        raise FAQServiceError("Question and answer must not be empty.")

    plan = await _resolve_plan(guild_id)
    limit = _limit_for_plan(plan)
    current = await storage.count_entries(guild_id)
    if limit is not None and current >= limit:
        raise FAQLimitError(limit, plan)

    entry = await storage.insert_entry(guild_id, normalized_question, normalized_answer)
    if entry is None:
        raise FAQServiceError("Failed to persist FAQ entry.")

    if vector_store.is_available():
        vector_id = await vector_store.add_entry(entry)
        if vector_id is not None:
            entry.vector_id = vector_id
            await storage.update_vector_id(guild_id, entry.entry_id, vector_id)
            _remove_from_backfill(entry)
        else:
            _queue_vector_backfill(entry)
    else:
        _queue_vector_backfill(entry)
    return entry


async def delete_faq_entry(guild_id: int, entry_id: int) -> FAQEntry:
    entry = await storage.delete_entry(guild_id, entry_id)
    if entry is None:
        raise FAQEntryNotFoundError(entry_id)

    if entry.vector_id is not None and vector_store.is_available():
        await vector_store.delete_vector(entry.vector_id)
    _remove_from_backfill(entry)
    return entry
