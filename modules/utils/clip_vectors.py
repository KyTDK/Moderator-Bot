"""
Thread-safe CLIP vector store backed by SQLite  →  FAISS IVF-GPU index
──────────────────────────────────────────────────────────────────────
Schema
------
CREATE TABLE vectors (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    vec       BLOB    NOT NULL,              -- float32[768] → .tobytes()
    category  TEXT,
    meta      TEXT                           -- full JSON for this image
);

• PRAGMA journal_mode=WAL for concurrency
• Each INSERT is atomic; multiple threads are fine
• FAISS uses `add_with_ids()` so DB ids == index ids
"""

import json
import sqlite3
import numpy as np
import faiss
import torch
from PIL import Image
from collections import defaultdict
from threading import Lock
from transformers import CLIPProcessor, CLIPModel
from typing import List, Dict

DIM         = 768
NLIST       = 64
MIN_TRAIN   = max(32, NLIST * 40)

DB_PATH     = "clip_vectors.sqlite"
INDEX_PATH  = "vector.index"

device = "cuda" if torch.cuda.is_available() else "cpu"
model  = CLIPModel.from_pretrained("openai/clip-vit-large-patch14").to(device)
proc   = CLIPProcessor.from_pretrained("openai/clip-vit-large-patch14")
gpu_res = faiss.StandardGpuResources()

_write_lock = Lock()

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA synchronous=NORMAL")

db.execute("""
CREATE TABLE IF NOT EXISTS vectors (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    vec      BLOB    NOT NULL,
    category TEXT,
    meta     TEXT
)
""")
db.commit()

def _new_gpu_index() -> faiss.GpuIndexIVFFlat:
    cfg = faiss.GpuIndexIVFFlatConfig()
    cfg.device = 0
    cfg.indicesOptions = faiss.INDICES_64_BIT 
    g = faiss.GpuIndexIVFFlat(
        gpu_res, DIM, NLIST, faiss.METRIC_INNER_PRODUCT, cfg
    )
    g.nprobe = max(1, NLIST // 4)
    return g

index = _new_gpu_index()

def _rebuild_index():
    """Load *all* vectors from DB → train & add to GPU index."""
    cur = db.execute("SELECT id, vec FROM vectors")
    rows = cur.fetchall()
    if not rows:
        return
    ids, vecs = zip(*rows)
    vecs = np.vstack([np.frombuffer(b, np.float32) for b in vecs]).astype("float32")
    ids  = np.asarray(ids, np.int64)

    faiss.normalize_L2(vecs)
    index.reset()
    if not index.is_trained:
        index.train(vecs)
    index.add_with_ids(vecs, ids)
    print(f"[INDEX] Rebuilt with {index.ntotal} vectors")

_rebuild_index()

def _persist_index():
    """Persist a CPU copy of the index to disk."""
    cpu = faiss.index_gpu_to_cpu(index)
    faiss.write_index(cpu, INDEX_PATH)

def embed(img: Image.Image) -> np.ndarray:
    t = proc(images=img, return_tensors="pt").to(device)
    with torch.no_grad():
        v = model.get_image_features(**t).cpu().numpy().astype("float32")
    faiss.normalize_L2(v)
    return v

def add_vector(img: Image.Image, metadata: dict):
    """Embed image, store in SQLite, and add to FAISS index."""
    vec = embed(img)
    blob = vec.tobytes()
    category = metadata.get("category")

    with _write_lock:
        cur = db.execute(
            "INSERT INTO vectors(vec, category, meta) VALUES (?,?,?)",
            (blob, category, json.dumps(metadata))
        )
        db.commit()
        row_id = cur.lastrowid

        if index.is_trained:
            index.add_with_ids(vec, np.array([row_id], np.int64))
            _persist_index()
        else:
            _maybe_train()

def _maybe_train():
    if index.is_trained:
        return
    count = db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
    if count < MIN_TRAIN:
        return
    _rebuild_index()
    _persist_index()

def query_similar(img: Image.Image,
                  threshold: float = 0.80,
                  k: int = 20,
                  min_votes: int = 1) -> List[Dict]:
    if not index.is_trained:
        return []

    vec = embed(img)
    distances, indices = index.search(vec, k)

    votes: dict[str, list[float]] = defaultdict(list)
    top_hit: dict[str, Dict] = {}

    for sim, idx in zip(distances[0], indices[0]):
        if idx < 0 or sim < threshold:
            continue

        row = db.execute(
            "SELECT category, meta FROM vectors WHERE id=?",
            (int(idx),)
        ).fetchone()
        if not row:
            continue

        category, meta_json = row
        hit = {**json.loads(meta_json), "similarity": float(sim)}

        votes[category].append(sim)

        if category not in top_hit or sim > top_hit[category]["similarity"]:
            top_hit[category] = hit

    valid_categories = {cat for cat, sims in votes.items() 
                        if len(sims) >= min_votes}

    result = [top_hit[cat] for cat in valid_categories]
    result.sort(key=lambda h: h["similarity"], reverse=True)
    return result