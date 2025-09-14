import json
import logging
import math
import os
from collections import defaultdict
from threading import Lock
from typing import Dict, List

import numpy as np
import torch
from PIL import Image
from dotenv import load_dotenv
from pymilvus import Collection, connections, utility
from transformers import CLIPModel, CLIPProcessor

load_dotenv()

MILVUS_HOST = os.getenv("MILVUS_HOST", "localhost")
MILVUS_PORT = os.getenv("MILVUS_PORT", "19530")
COLLECTION_NAME = "clip_vectors"
VECTOR_FIELD = "vector"

# Index naming & dynamic rebuild thresholds
INDEX_NAME = "ivf_flat_ip"
COVERAGE_MIN = 0.70          # rebuild if indexed_rows / total_rows < 0.70
MIN_NEW_ROWS = 5_000         # avoid rebuilds for small deltas

# Defaults if needed before dynamic tuning
DEFAULT_NLIST = 128

log = logging.getLogger(__name__)

connections.connect("default", host=MILVUS_HOST, port=MILVUS_PORT)
collection = Collection(COLLECTION_NAME)

def _choose_nlist(n_rows: int) -> int:
    """
    Heuristic for IVF nlist: ~8 * sqrt(N), clamped.
    """
    if n_rows <= 0:
        return DEFAULT_NLIST
    return max(64, min(4096, int(round(8.0 * math.sqrt(n_rows)))))

def _choose_nprobe(nlist: int) -> int:
    """
    Heuristic for IVF nprobe: ~12.5% of nlist (nlist//8), clamped.
    """
    return max(1, min(nlist, nlist // 8))

def _current_index_nlist_safe() -> int | None:
    """
    Try to read the current index's nlist; return None if unavailable.
    """
    try:
        infos = collection.describe_index(INDEX_NAME)
        params = (infos[0].get("index_parameters") or infos[0].get("params") or {})
        return int(params.get("nlist"))
    except Exception:
        return None

def _create_index(nlist: int) -> None:
    """
    Create IVF_FLAT index with the given nlist.
    """
    collection.create_index(
        field_name=VECTOR_FIELD,
        index_params={
            "index_type": "IVF_FLAT",
            "metric_type": "IP",
            "params": {"nlist": nlist},
        },
        index_name=INDEX_NAME,
    )

def _maybe_rebuild_index() -> int:
    """
    Rebuild the IVF index if:
      - No index exists, OR
      - Coverage < COVERAGE_MIN and at least MIN_NEW_ROWS pending, OR
      - nlist should scale significantly (>= 25% change).

    Returns the nprobe to use (derived from the chosen nlist).
    """
    # Ensure latest segments are accounted for
    try:
        collection.flush()
    except Exception:
        pass

    # Get build progress if available
    try:
        prog = utility.get_index_build_progress(COLLECTION_NAME, INDEX_NAME)
        total = int(prog.get("total_rows", 0))
        indexed = int(prog.get("indexed_rows", 0))
    except Exception:
        total = int(collection.num_entities)
        indexed = 0

    # Keep totals in sync with collection state
    total = max(total, int(collection.num_entities))
    desired_nlist = _choose_nlist(total)

    need_build = not collection.has_index()
    if total > 0:
        coverage = (indexed / total) if total else 1.0
        new_rows = total - indexed
        if (coverage < COVERAGE_MIN) and (new_rows >= MIN_NEW_ROWS):
            need_build = True

    # Rebuild if the current nlist is far from desired (>= 25% swing)
    if collection.has_index() and not need_build:
        current_nlist = _current_index_nlist_safe()
        if current_nlist is None:
            need_build = True
        else:
            diff_ratio = abs(current_nlist - desired_nlist) / max(1, current_nlist)
            if diff_ratio > 0.25:
                need_build = True

    if need_build:
        log.info(
            "Rebuilding index: total=%d, indexed=%d, desired nlist=%d",
            total, indexed, desired_nlist
        )
        try:
            collection.release()
        except Exception:
            pass
        try:
            collection.drop_index(index_name=INDEX_NAME)
        except Exception:
            pass
        _create_index(desired_nlist)
    else:
        log.debug(
            "Index OK; skipping rebuild. total=%d, indexed=%d, desired nlist=%d",
            total, indexed, desired_nlist
        )

    # Load with tuned nprobe
    nprobe = _choose_nprobe(desired_nlist)
    try:
        collection.load()
    except Exception:
        try:
            collection.release()
        except Exception:
            pass
        collection.load()

    log.info("Collection loaded with nprobe=%d", nprobe)
    return nprobe

# Build/validate index once at import
NPROBE = _maybe_rebuild_index()

_model: CLIPModel | None = None
_proc: CLIPProcessor | None = None
_device: str | None = None
_init_lock = Lock()
_write_lock = Lock()

def _ensure_clip_loaded() -> None:
    """
    Lazily load CLIP model & processor (thread-safe).
    """
    global _model, _proc, _device
    if _model is not None and _proc is not None and _device is not None:
        return
    with _init_lock:
        if _model is not None and _proc is not None and _device is not None:
            return
        dev = "cuda" if torch.cuda.is_available() else "cpu"
        model = CLIPModel.from_pretrained("openai/clip-vit-large-patch14")
        proc = CLIPProcessor.from_pretrained("openai/clip-vit-large-patch14")
        _model = model.to(dev)
        _proc = proc
        _device = dev

def embed(img: Image.Image) -> np.ndarray:
    """
    Return L2-normalized CLIP image embedding (float32, shape=(1, D)).
    """
    _ensure_clip_loaded()
    t = _proc(images=img, return_tensors="pt")  # type: ignore[arg-type]
    t = t.to(_device) if hasattr(t, "to") else t
    with torch.no_grad():
        v = _model.get_image_features(**t).cpu().numpy().astype("float32")  # type: ignore[union-attr]
    v = v / np.linalg.norm(v, axis=1, keepdims=True)
    return v

def add_vector(img: Image.Image, metadata: dict) -> None:
    """
    Insert a single image vector and associated metadata.
    """
    vec = embed(img)[0]
    category = metadata.get("category") or "safe"
    meta = json.dumps(metadata)
    with _write_lock:
        data = [[vec], [category], [meta]]
        collection.insert(data)

def query_similar(
    img: Image.Image,
    threshold: float = 0.80,
    k: int = 20,
    min_votes: int = 1
) -> List[Dict]:
    """
    Search for similar images and aggregate by category with a voting heuristic.
    """
    vec = embed(img)[0]
    search_params = {"metric_type": "IP", "params": {"nprobe": NPROBE}}
    results = collection.search(
        data=[vec],
        anns_field=VECTOR_FIELD,
        param=search_params,
        limit=k,
        output_fields=["category", "meta"],
    )

    if not results or not results[0]:
        return []

    votes: Dict[str, List[float]] = defaultdict(list)
    top_hit: Dict[str, Dict] = {}

    for hit in results[0]:
        sim = float(hit.score)
        if sim < threshold:
            continue

        category = hit.entity.get("category")
        meta_json = hit.entity.get("meta")
        meta = json.loads(meta_json) if meta_json else {}
        meta["similarity"] = sim

        votes[category].append(sim)
        if category not in top_hit or sim > top_hit[category]["similarity"]:
            top_hit[category] = meta

    valid_categories = {cat for cat, sims in votes.items() if len(sims) >= min_votes}
    result = [top_hit[cat] for cat in valid_categories]
    result.sort(key=lambda h: h["similarity"], reverse=True)
    return result
