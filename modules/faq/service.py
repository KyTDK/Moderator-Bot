from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Optional

from modules.config.premium_plans import (
    PLAN_CORE,
    PLAN_FREE,
    PLAN_PRO,
    PLAN_ULTRA,
    PLAN_DISPLAY_NAMES,
)
from modules.faq.constants import (
    DEFAULT_FAQ_SIMILARITY_THRESHOLD,
    MAX_FAQ_SIMILARITY_THRESHOLD,
    MIN_FAQ_SIMILARITY_THRESHOLD,
)
from modules.faq.models import FAQEntry, FAQSearchResult
from modules.faq import storage, vector_store
from modules.faq.settings_keys import FAQ_THRESHOLD_SETTING
from modules.utils import mysql

_MIN_WORDS = 2
_MAX_CHUNKS = 8
_CHUNK_SIZE = 12
_CHUNK_OVERLAP = 8

_URL_RE = re.compile(r"https?://\S+")
_MENTION_RE = re.compile(r"<@!?[0-9]+>")
_WHITESPACE_RE = re.compile(r"\s+")

FAQ_LIMITS: dict[str, Optional[int]] = {
    PLAN_FREE: 5,
    PLAN_CORE: 20,
    PLAN_PRO: 50,
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


def _normalise_text(text: str) -> str:
    without_mentions = _MENTION_RE.sub("", text or "")
    without_urls = _URL_RE.sub("", without_mentions)
    collapsed = _WHITESPACE_RE.sub(" ", without_urls)
    return collapsed.strip()


def _chunk_text(text: str) -> list[str]:
    words = text.split()
    if len(words) <= _CHUNK_SIZE or _CHUNK_SIZE <= 0:
        return [text] if text else []

    chunks: list[str] = []
    step = max(1, _CHUNK_SIZE - _CHUNK_OVERLAP)
    for i in range(0, len(words), step):
        chunk = " ".join(words[i : i + _CHUNK_SIZE])
        if chunk:
            chunks.append(chunk)
        if len(chunks) >= _MAX_CHUNKS or i + _CHUNK_SIZE >= len(words):
            break
    return chunks


async def _resolve_plan(guild_id: int) -> str:
    plan = await mysql.resolve_guild_plan(guild_id)
    return plan or PLAN_FREE


def _limit_for_plan(plan: str) -> Optional[int]:
    return FAQ_LIMITS.get(plan, FAQ_LIMITS[PLAN_FREE])


def _coerce_threshold(value: Any) -> float:
    if value is None:
        return DEFAULT_FAQ_SIMILARITY_THRESHOLD
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return DEFAULT_FAQ_SIMILARITY_THRESHOLD
    if numeric != numeric:  # NaN check
        return DEFAULT_FAQ_SIMILARITY_THRESHOLD
    if numeric < MIN_FAQ_SIMILARITY_THRESHOLD:
        return MIN_FAQ_SIMILARITY_THRESHOLD
    if numeric > MAX_FAQ_SIMILARITY_THRESHOLD:
        return MAX_FAQ_SIMILARITY_THRESHOLD
    return numeric


async def list_faq_entries(guild_id: int) -> list[FAQEntry]:
    return await storage.fetch_entries(guild_id)


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
    return entry


async def delete_faq_entry(guild_id: int, entry_id: int) -> FAQEntry:
    entry = await storage.delete_entry(guild_id, entry_id)
    if entry is None:
        raise FAQEntryNotFoundError(entry_id)

    if entry.vector_id is not None and vector_store.is_available():
        await vector_store.delete_vector(entry.vector_id)
    return entry


async def find_best_faq_answer(
    guild_id: int,
    message_content: str,
    *,
    threshold: float | None = None,
) -> Optional[FAQSearchResult]:
    normalized = _normalise_text(message_content)
    if not normalized:
        return None

    words = normalized.split()
    if len(words) < _MIN_WORDS:
        return None

    chunks = _chunk_text(normalized)
    if not chunks:
        return None

    effective_threshold = _coerce_threshold(threshold)
    if threshold is None:
        threshold_setting = await mysql.get_settings(guild_id, FAQ_THRESHOLD_SETTING)
        effective_threshold = _coerce_threshold(threshold_setting)

    if not vector_store.is_available():
        return await _fallback_find_best_answer(
            guild_id,
            normalized,
            effective_threshold,
        )

    results = vector_store.query_chunks(
        chunks,
        guild_id=guild_id,
        threshold=effective_threshold,
        k=5,
    )

    best_entry_id: Optional[int] = None
    best_similarity = 0.0
    best_chunk: Optional[str] = None

    for chunk, match_group in zip(chunks, results or []):
        for match in match_group:
            entry_id = match.get("entry_id")
            similarity_raw = match.get("similarity", 0)
            if entry_id is None:
                continue
            try:
                similarity = float(similarity_raw)
            except (TypeError, ValueError):
                continue
            if similarity < effective_threshold:
                continue
            numeric_entry_id = int(entry_id)
            if best_entry_id is None or similarity > best_similarity:
                best_entry_id = numeric_entry_id
                best_similarity = similarity
                best_chunk = chunk

    if best_entry_id is None:
        return None

    entry = await storage.fetch_entry(guild_id, best_entry_id)
    if entry is None:
        return None

    return FAQSearchResult(
        entry=entry,
        similarity=best_similarity,
        source_chunk=best_chunk,
        used_fallback=False,
    )


async def _fallback_find_best_answer(
    guild_id: int,
    normalized_message: str,
    effective_threshold: float,
) -> Optional[FAQSearchResult]:
    entries = await storage.fetch_entries(guild_id)
    if not entries:
        return None

    lowered_message = normalized_message.lower()
    fallback_threshold = max(
        MIN_FAQ_SIMILARITY_THRESHOLD,
        min(effective_threshold, 0.95) - 0.05,
    )

    best_entry: FAQEntry | None = None
    best_score = 0.0

    for entry in entries:
        normalized_question = _normalise_text(entry.question)
        if not normalized_question:
            continue

        lowered_question = normalized_question.lower()
        if lowered_question in lowered_message or lowered_message in lowered_question:
            score = 1.0
        else:
            score = SequenceMatcher(None, lowered_question, lowered_message).ratio()

        if score < fallback_threshold:
            continue
        if best_entry is None or score > best_score:
            best_entry = entry
            best_score = score

    if best_entry is None:
        return None

    return FAQSearchResult(
        entry=best_entry,
        similarity=best_score,
        source_chunk=normalized_message,
        used_fallback=True,
    )
