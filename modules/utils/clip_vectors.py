import json
import logging
import numpy as np
import torch
from PIL import Image
from collections import defaultdict
from threading import Lock
from transformers import CLIPProcessor, CLIPModel
from typing import List, Dict

from pymilvus import (
    connections, Collection
)

import os
from dotenv import load_dotenv

load_dotenv()
# Milvus connection/config
MILVUS_HOST = os.getenv("MILVUS_HOST", "localhost")
MILVUS_PORT = os.getenv("MILVUS_PORT", "19530")
COLLECTION_NAME = "clip_vectors"

log = logging.getLogger(__name__)
connections.connect("default", host=MILVUS_HOST, port=MILVUS_PORT)
collection = Collection(COLLECTION_NAME)

def _suggest_ivf_params(n_vectors: int) -> tuple[int, int]:
    # NLIST ≈ 2–4 * sqrt(N); clamp to [256, 4096] for practicality
    import math
    nlist = int(max(256, min(4096, round(4 * math.sqrt(max(n_vectors, 1))))))
    # NPROBE ≈ 3% of NLIST=
    nprobe = max(8, min(nlist, int(round(nlist * 0.03))))
    # round nprobe to nearest power-of-two-ish for convenience
    pow2 = 1 << (nprobe - 1).bit_length()
    nprobe = min(pow2, nlist)
    return nlist, nprobe

# After connecting:
n_vectors = collection.num_entities
NLIST, NPROBE = _suggest_ivf_params(n_vectors)
log.info(f"Using IVF params: NLIST={NLIST}, NPROBE={NPROBE} for N={n_vectors}")

if not collection.has_index():
    collection.create_index(
        field_name="vector",
        index_params={"index_type": "IVF_FLAT", "metric_type": "IP", "params": {"nlist": NLIST}}
    )
collection.load()

# Lazy-load CLIP model and processor on first use to avoid blocking startup
_model = None
_proc = None
_device = None
_init_lock = Lock()

_write_lock = Lock()

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

def add_vector(img: Image.Image, metadata: dict):
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
    vec = embed(img)[0]
    search_params = {
        "metric_type": "IP", # inner product
        "params": {"nprobe": NPROBE}
    }

    results = collection.search(
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

        votes[category].append(sim)
        if category not in top_hit or sim > top_hit[category]["similarity"]:
            top_hit[category] = meta

    valid_categories = {cat for cat, sims in votes.items() 
                        if len(sims) >= min_votes}

    result = [top_hit[cat] for cat in valid_categories]
    result.sort(key=lambda h: h["similarity"], reverse=True)
    return result
