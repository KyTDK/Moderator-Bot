from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Sequence

from modules.faq.models import FAQEntry
from modules.utils.text_vectors import MILVUS_HOST, MILVUS_PORT, embed_text_batch
from modules.utils.vector_spaces import MilvusVectorSpace, VectorDeleteStats

log = logging.getLogger(__name__)

COLLECTION_NAME = "faq_text_vectors"

_FAQ_VECTOR_SPACE = MilvusVectorSpace(
    collection_name=COLLECTION_NAME,
    dim=384,
    embed_batch=embed_text_batch,
    description="Embeddings for guild FAQ questions",
    metric_type="IP",
    host=MILVUS_HOST,
    port=MILVUS_PORT,
    logger=log,
)


def _category_for_guild(guild_id: int) -> str:
    return f"faq:{guild_id}"


def is_available() -> bool:
    return _FAQ_VECTOR_SPACE.is_available()


def is_fallback_active() -> bool:
    return _FAQ_VECTOR_SPACE.is_fallback_active()


async def add_entry(entry: FAQEntry) -> int | None:
    metadata = {
        "guild_id": entry.guild_id,
        "entry_id": entry.entry_id,
        "question": entry.question,
        "answer": entry.answer,
        "category": _category_for_guild(entry.guild_id),
    }
    return await asyncio.to_thread(
        _FAQ_VECTOR_SPACE.add_vector,
        entry.question,
        metadata,
    )


async def delete_vector(vector_id: int) -> VectorDeleteStats | None:
    return await _FAQ_VECTOR_SPACE.delete_vectors([vector_id])


def query_chunks(
    chunks: Sequence[str],
    *,
    guild_id: int,
    threshold: float,
    k: int = 5,
) -> List[List[Dict[str, Any]]]:
    if not chunks:
        return []

    category = _category_for_guild(guild_id)
    return _FAQ_VECTOR_SPACE.query_similar_batch(
        chunks,
        threshold=threshold,
        k=k,
        min_votes=1,
        categories=[category],
    )


def query_text(
    text: str,
    *,
    guild_id: int,
    threshold: float,
    k: int = 5,
) -> List[Dict[str, Any]]:
    if not text:
        return []
    category = _category_for_guild(guild_id)
    return _FAQ_VECTOR_SPACE.query_similar(
        text,
        threshold=threshold,
        k=k,
        min_votes=1,
        categories=[category],
    )


__all__ = [
    "COLLECTION_NAME",
    "add_entry",
    "delete_vector",
    "query_chunks",
    "query_text",
    "is_available",
    "is_fallback_active",
]
