from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(slots=True)
class FAQEntry:
    """Persisted FAQ entry configured by a guild."""

    guild_id: int
    entry_id: int
    question: str
    answer: str
    vector_id: Optional[int] = None


@dataclass(slots=True)
class FAQSearchResult:
    """Result produced by a similarity lookup for an FAQ answer."""

    entry: FAQEntry
    similarity: float
    source_chunk: Optional[str] = None
