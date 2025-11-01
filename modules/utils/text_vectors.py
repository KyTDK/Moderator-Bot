from __future__ import annotations

import logging
import os
from threading import Lock
from typing import Any, Iterable, List, Sequence

import numpy as np
from dotenv import load_dotenv
try:
    from sentence_transformers import SentenceTransformer
except ImportError:  # pragma: no cover - optional dependency
    SentenceTransformer = None  # type: ignore[assignment]

from .vector_spaces import MilvusVectorSpace, VectorDeleteStats

load_dotenv()

MILVUS_HOST = os.getenv("MILVUS_HOST", "localhost")
MILVUS_PORT = os.getenv("MILVUS_PORT", "19530")
COLLECTION_NAME = "text_vectors"

log = logging.getLogger(__name__)

_text_model: SentenceTransformer | None = None
_text_lock = Lock()
_TEXT_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
_model_available = SentenceTransformer is not None


def _ensure_text_model() -> SentenceTransformer:
    if not _model_available:
        raise RuntimeError("sentence-transformers package is required for text vector support")

    global _text_model
    if _text_model is not None:
        return _text_model

    with _text_lock:
        if _text_model is not None:
            return _text_model
        model = SentenceTransformer(_TEXT_MODEL_NAME, device="cpu")
        _text_model = model
        return model


def embed_text_batch(texts: Sequence[str]) -> np.ndarray:
    if not texts:
        return np.empty((0, 0), dtype="float32")

    if not _model_available:
        log.debug("Text vectors requested but sentence-transformers is not installed; returning empty result")
        return np.empty((0, 0), dtype="float32")

    model = _ensure_text_model()
    vectors = model.encode(list(texts), convert_to_numpy=True, normalize_embeddings=True)
    if vectors.dtype != np.float32:
        vectors = vectors.astype("float32")
    return vectors


_TEXT_VECTOR_SPACE = MilvusVectorSpace(
    collection_name=COLLECTION_NAME,
    dim=384,
    embed_batch=embed_text_batch,
    description="Text embeddings for content moderation",
    metric_type="IP",
    host=MILVUS_HOST,
    port=MILVUS_PORT,
    logger=log,
)


def register_failure_callback(callback) -> None:
    _TEXT_VECTOR_SPACE.register_failure_callback(callback)


def is_available() -> bool:
    return _TEXT_VECTOR_SPACE.is_available()


def is_fallback_active() -> bool:
    return _TEXT_VECTOR_SPACE.is_fallback_active()


def get_last_error() -> Exception | None:
    return _TEXT_VECTOR_SPACE.get_last_error()


def add_vector(text: str, metadata: dict[str, Any]) -> None:
    _TEXT_VECTOR_SPACE.add_vector(text, metadata)


async def delete_vectors(ids: Iterable[int]) -> VectorDeleteStats | None:
    return await _TEXT_VECTOR_SPACE.delete_vectors(ids)


def query_similar(
    text: str,
    threshold: float = 0.70,
    k: int = 20,
    min_votes: int = 1,
) -> List[dict[str, Any]]:
    return _TEXT_VECTOR_SPACE.query_similar(
        text,
        threshold=threshold,
        k=k,
        min_votes=min_votes,
    )


def query_similar_batch(
    texts: Sequence[str],
    threshold: float = 0.70,
    k: int = 20,
    min_votes: int = 1,
) -> List[List[dict[str, Any]]]:
    return _TEXT_VECTOR_SPACE.query_similar_batch(
        texts,
        threshold=threshold,
        k=k,
        min_votes=min_votes,
    )


__all__ = [
    "COLLECTION_NAME",
    "register_failure_callback",
    "is_available",
    "is_fallback_active",
    "get_last_error",
    "embed_text_batch",
    "add_vector",
    "delete_vectors",
    "query_similar",
    "query_similar_batch",
    "VectorDeleteStats",
]
