import asyncio
import json
import logging
import math
import os
from collections import defaultdict
from threading import Event, Lock, Thread
from typing import Dict, Iterable, List, Optional

import numpy as np
import torch
from PIL import Image
from dotenv import load_dotenv
from pymilvus import Collection, connections
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
    global _collection, _collection_error, NLIST, NPROBE, _logged_ivf_params

    try:
        connections.connect("default", host=MILVUS_HOST, port=MILVUS_PORT)
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
    except Exception as exc:  # pragma: no cover - defensive logging
        with _collection_state_lock:
            _collection = None
            _collection_error = exc
        log.exception(
            "Failed to initialize Milvus collection '%s': %s",
            COLLECTION_NAME,
            exc,
        )
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
    global _collection_not_ready_warned

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
            log.error(
                "Milvus collection '%s' failed to initialize: %s",
                COLLECTION_NAME,
                _collection_error,
            )
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


def embed(img: Image.Image) -> np.ndarray:
    _ensure_clip_loaded()
    t = _proc(images=img, return_tensors="pt")
    # BatchEncoding supports .to(device)
    t = t.to(_device) if hasattr(t, "to") else t
    with torch.no_grad():
        v = _model.get_image_features(**t).cpu().numpy().astype("float32")
    v = v / np.linalg.norm(v, axis=1, keepdims=True)
    return v


def add_vector(img: Image.Image, metadata: dict) -> None:
    coll = _get_collection(timeout=60.0)
    if coll is None:
        log.warning("Skipping vector insert; Milvus collection is not ready")
        return
    vec = embed(img)[0]
    category = metadata.get("category") or "safe"
    meta = json.dumps(metadata)
    with _write_lock:
        data = [[vec], [category], [meta]]
        coll.insert(data)


async def delete_vectors(ids: Iterable[int]) -> None:
    """Remove vectors identified by their primary keys."""
    coll = _get_collection(timeout=30.0)
    if coll is None:
        log.warning("Unable to delete vectors; Milvus collection is not ready")
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
    coll = _get_collection(timeout=30.0)
    if coll is None:
        log.warning("Vector search skipped; Milvus collection is not ready")
        return []

    vec = embed(img)[0]
    search_params = {
        "metric_type": "IP",  # inner product
        "params": {"nprobe": NPROBE}
    }

    results = coll.search(
        data=[vec],
        anns_field="vector",
        param=search_params,
        limit=k,
        output_fields=["category", "meta"]
    )
    if not results or not results[0]:
        return []

    votes: dict[str, list[float]] = defaultdict(list)
    top_hit: dict[str, Dict] = {}

    for hit in results[0]:
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

    valid_categories = {cat for cat, sims in votes.items()
                        if len(sims) >= min_votes}

    result = [top_hit[cat] for cat in valid_categories]
    result.sort(key=lambda h: h["similarity"], reverse=True)
    return result