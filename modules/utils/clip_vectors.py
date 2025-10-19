import asyncio
import inspect
import json
import logging
import math
import os
from collections import defaultdict
from threading import Event, Lock, Thread
from typing import Callable, Dict, Iterable, List, Optional, Sequence

import numpy as np
import torch
from PIL import Image
from dotenv import load_dotenv
from pymilvus import (
    Collection,
    CollectionSchema,
    FieldSchema,
    DataType,
    connections,
    utility,
)
try:
    from pymilvus.exceptions import MilvusException
except ImportError:  # pragma: no cover - older pymilvus versions
    MilvusException = Exception
from transformers import CLIPModel, CLIPProcessor

load_dotenv()
# Milvus connection/config
MILVUS_HOST = os.getenv("MILVUS_HOST", "localhost")
MILVUS_PORT = os.getenv("MILVUS_PORT", "19530")
COLLECTION_NAME = "clip_vectors"

log = logging.getLogger(__name__)

# Global Milvus state is initialized in the background so the bot startup isn't blocked.
_collection: Optional[Collection] = None
_collection_error: Optional[Exception] = None
_collection_ready = Event()
_collection_init_started = Event()
_collection_state_lock = Lock()
NLIST = 1024
NPROBE = 64
_logged_ivf_params = False
_collection_not_ready_warned = False
_collection_error_logged = False
_failure_callbacks: list[tuple[Callable[[Exception], object], Optional[asyncio.AbstractEventLoop]]] = []
_last_notified_error_key: Optional[str] = None
_fallback_active = False
_vector_insert_warned = False
_vector_search_warned = False
_vector_delete_warned = False


def _make_error_key(exc: Exception) -> str:
    return f"{type(exc).__name__}:{exc}"


def _run_failure_callback(
    callback: Callable[[Exception], object],
    loop: Optional[asyncio.AbstractEventLoop],
    exc: Exception,
) -> None:
    try:
        result = callback(exc)
    except Exception:  # pragma: no cover - defensive logging
        log.exception("Milvus failure callback raised")
        return

    if inspect.isawaitable(result):
        if loop and not loop.is_closed():
            asyncio.run_coroutine_threadsafe(result, loop)
        else:
            log.debug(
                "Discarded Milvus failure coroutine callback; no running event loop available",
            )


def _notify_failure(exc: Exception, *, force: bool = False) -> None:
    global _last_notified_error_key, _fallback_active
    key = _make_error_key(exc)

    with _collection_state_lock:
        if not force and _last_notified_error_key == key:
            return
        _last_notified_error_key = key
        callbacks = list(_failure_callbacks)
        _fallback_active = True

    for callback, loop in callbacks:
        _run_failure_callback(callback, loop, exc)


def register_failure_callback(callback: Callable[[Exception], object]) -> None:
    """Register a callback that runs when the Milvus collection fails."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    with _collection_state_lock:
        _failure_callbacks.append((callback, loop))
        current_error = _collection_error

    if current_error:
        _run_failure_callback(callback, loop, current_error)


def is_available() -> bool:
    """Return True when the Milvus collection is ready for use."""
    with _collection_state_lock:
        return _collection is not None


def is_fallback_active() -> bool:
    """Return True if Milvus has failed and the OpenAI fallback should be used."""
    with _collection_state_lock:
        return _fallback_active


def get_last_error() -> Optional[Exception]:
    with _collection_state_lock:
        return _collection_error

_model = None
_proc = None
_device = None
_init_lock = Lock()
_write_lock = Lock()


def _suggest_ivf_params(n_vectors: int) -> tuple[int, int]:
    """Suggest reasonable IVF parameters for the current collection size."""
    nlist = int(max(256, min(4096, round(4 * math.sqrt(max(n_vectors, 1))))))
    nprobe = max(8, min(nlist, int(round(nlist * 0.03))))
    pow2 = 1 << (nprobe - 1).bit_length()
    nprobe = min(pow2, nlist)
    return nlist, nprobe


def _initialize_collection() -> None:
    """Connect to Milvus, ensure the index exists, and load the collection."""
    global _collection
    global _collection_error
    global NLIST, NPROBE, _logged_ivf_params
    global _collection_error_logged, _last_notified_error_key, _fallback_active
    global _vector_insert_warned, _vector_search_warned, _vector_delete_warned

    try:
        connections.connect("default", host=MILVUS_HOST, port=MILVUS_PORT)
        if not utility.has_collection(COLLECTION_NAME):
            log.info("Milvus collection '%s' missing; creating", COLLECTION_NAME)
            schema = CollectionSchema(
                fields=[
                    FieldSchema(
                        name="id",
                        dtype=DataType.INT64,
                        is_primary=True,
                        auto_id=True,
                    ),
                    FieldSchema(
                        name="vector",
                        dtype=DataType.FLOAT_VECTOR,
                        dim=768,
                    ),
                    FieldSchema(
                        name="category",
                        dtype=DataType.VARCHAR,
                        max_length=32,
                    ),
                    FieldSchema(
                        name="meta",
                        dtype=DataType.VARCHAR,
                        max_length=8192,
                    ),
                ]
            )
            Collection(
                name=COLLECTION_NAME,
                schema=schema,
                description="CLIP embeddings for content moderation",
            )
        coll = Collection(COLLECTION_NAME)
        n_vectors = coll.num_entities
        NLIST, NPROBE = _suggest_ivf_params(n_vectors)
        if not _logged_ivf_params:
            log.info(
                "Using IVF params: NLIST=%s, NPROBE=%s for N=%s",
                NLIST,
                NPROBE,
                n_vectors,
            )
            _logged_ivf_params = True
        if not coll.has_index():
            log.info(
                "Index missing for collection '%s'; building IVF_FLAT with nlist=%s",
                COLLECTION_NAME,
                NLIST,
            )
            coll.create_index(
                field_name="vector",
                index_params={
                    "index_type": "IVF_FLAT",
                    "metric_type": "IP",
                    "params": {"nlist": NLIST},
                },
            )
        coll.load()
        with _collection_state_lock:
            _collection = coll
            _collection_error = None
            _fallback_active = False
            _collection_error_logged = False
            _last_notified_error_key = None
            _vector_insert_warned = False
            _vector_search_warned = False
            _vector_delete_warned = False
    except Exception as exc:  # pragma: no cover - defensive logging
        with _collection_state_lock:
            _collection = None
            _collection_error = exc
            _collection_error_logged = False
            _vector_insert_warned = False
            _vector_search_warned = False
            _vector_delete_warned = False
        log.exception(
            "Failed to initialize Milvus collection '%s': %s",
            COLLECTION_NAME,
            exc,
        )
        _notify_failure(exc)
    finally:
        _collection_ready.set()


def _ensure_collection_initializer_started() -> None:
    """Start the background initializer once."""
    if _collection_init_started.is_set():
        return
    with _collection_state_lock:
        if _collection_init_started.is_set():
            return
        thread = Thread(
            target=_initialize_collection,
            name="clip-vector-setup",
            daemon=True,
        )
        thread.start()
        _collection_init_started.set()


def _get_collection(timeout: Optional[float] = 30.0) -> Optional[Collection]:
    """Return the Milvus collection when ready, or None if unavailable."""
    global _collection_not_ready_warned, _collection_error_logged

    _ensure_collection_initializer_started()
    ready = _collection_ready.wait(timeout)
    if not ready:
        if not _collection_not_ready_warned:
            log.warning(
                "Milvus collection '%s' is still loading after %.1fs; vector ops deferred",
                COLLECTION_NAME,
                0.0 if timeout is None else timeout,
            )
            _collection_not_ready_warned = True
        return None

    with _collection_state_lock:
        if _collection is not None:
            return _collection
        if _collection_error is not None:
            if not _collection_error_logged:
                log.error(
                    "Milvus collection '%s' failed to initialize: %s",
                    COLLECTION_NAME,
                    _collection_error,
                )
                _collection_error_logged = True
        return None


def _ensure_clip_loaded() -> None:
    """Load CLIP model/processor on first use (thread-safe)."""
    global _model, _proc, _device
    if _model is not None and _proc is not None and _device is not None:
        return
    with _init_lock:
        if _model is not None and _proc is not None and _device is not None:
            return
        dev = "cuda" if torch.cuda.is_available() else "cpu"
        m = CLIPModel.from_pretrained("openai/clip-vit-large-patch14")
        p = CLIPProcessor.from_pretrained("openai/clip-vit-large-patch14")
        _model = m.to(dev)
        _proc = p
        _device = dev


def embed_batch(images: Sequence[Image.Image]) -> np.ndarray:
    """Return L2-normalised embeddings for a batch of images."""
    if not images:
        return np.empty((0, 0), dtype="float32")
    _ensure_clip_loaded()
    t = _proc(images=list(images), return_tensors="pt")
    t = t.to(_device) if hasattr(t, "to") else t
    with torch.no_grad():
        v = _model.get_image_features(**t).cpu().numpy().astype("float32")

    norms = np.linalg.norm(v, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    v = v / norms
    return v


def embed(img: Image.Image) -> np.ndarray:
    """Return a single image embedding (legacy helper)."""
    return embed_batch([img])


def add_vector(img: Image.Image, metadata: dict) -> None:
    global _vector_insert_warned
    coll = _get_collection(timeout=60.0)
    if coll is None:
        if not _vector_insert_warned:
            log.warning("Skipping vector insert; Milvus collection is not ready")
            _vector_insert_warned = True
        return
    vec = embed(img)[0]
    category = metadata.get("category") or "safe"
    meta = json.dumps(metadata)
    with _write_lock:
        data = [[vec], [category], [meta]]
        coll.insert(data)


async def delete_vectors(ids: Iterable[int]) -> None:
    """Remove vectors identified by their primary keys."""
    global _vector_delete_warned
    coll = _get_collection(timeout=30.0)
    if coll is None:
        if not _vector_delete_warned:
            log.warning("Unable to delete vectors; Milvus collection is not ready")
            _vector_delete_warned = True
        return
    normalized_ids = [int(value) for value in ids if value is not None]
    if not normalized_ids:
        return
    expr = "id in [" + ", ".join(str(value) for value in normalized_ids) + "]"

    def _delete_and_flush() -> None:
        with _write_lock:
            coll.delete(expr)
            try:
                coll.flush()
            except MilvusException as exc:  # pragma: no cover - defensive logging
                log.warning("Milvus flush after delete failed: %s", exc)

    await asyncio.to_thread(_delete_and_flush)


def query_similar(
    img: Image.Image,
    threshold: float = 0.80,
    k: int = 20,
    min_votes: int = 1
) -> List[Dict]:
    results = query_similar_batch([img], threshold=threshold, k=k, min_votes=min_votes)
    return results[0] if results else []


def query_similar_batch(
    images: Sequence[Image.Image],
    threshold: float = 0.80,
    k: int = 20,
    min_votes: int = 1,
) -> List[List[Dict]]:
    global _vector_search_warned
    coll = _get_collection(timeout=30.0)
    if coll is None:
        if not _vector_search_warned:
            log.warning("Vector search skipped; Milvus collection is not ready")
            _vector_search_warned = True
        return [[] for _ in images]

    if not images:
        return []

    vectors = embed_batch(images)
    if vectors.size == 0:
        return [[] for _ in images]

    search_params = {
        "metric_type": "IP",  # inner product
        "params": {"nprobe": NPROBE}
    }

    results = coll.search(
        data=[vec.tolist() for vec in vectors],
        anns_field="vector",
        param=search_params,
        limit=k,
        output_fields=["category", "meta"],
    )
    formatted: List[List[Dict]] = []
    for idx, vector_hits in enumerate(results or []):
        if not vector_hits:
            formatted.append([])
            continue

        votes: dict[str, list[float]] = defaultdict(list)
        top_hit: dict[str, Dict] = {}

        for hit in vector_hits:
            sim = float(hit.score)
            if sim < threshold:
                continue
            category = hit.entity.get("category")
            meta_json = hit.entity.get("meta")
            meta = json.loads(meta_json) if meta_json else {}
            meta["similarity"] = sim
            try:
                meta["vector_id"] = int(hit.id)
            except (TypeError, ValueError):
                meta["vector_id"] = hit.id

            votes[category].append(sim)
            if category not in top_hit or sim > top_hit[category]["similarity"]:
                top_hit[category] = meta

        valid_categories = {
            cat for cat, sims in votes.items() if len(sims) >= min_votes
        }
        batch_result = [top_hit[cat] for cat in valid_categories]
        batch_result.sort(key=lambda h: h["similarity"], reverse=True)
        formatted.append(batch_result)

    # Ensure one result list per image even if Milvus returned fewer batches.
    if len(formatted) < len(images):
        formatted.extend([[]] * (len(images) - len(formatted)))
    return formatted


__all__ = [
    "register_failure_callback",
    "is_available",
    "is_fallback_active",
    "get_last_error",
    "embed",
    "embed_batch",
    "add_vector",
    "delete_vectors",
    "query_similar",
    "query_similar_batch",
]
