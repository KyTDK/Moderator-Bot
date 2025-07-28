import json
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
DIM = 768
NLIST = 128
NPROBE = max(1, NLIST // 8)

connections.connect("default", host=MILVUS_HOST, port=MILVUS_PORT)
collection = Collection(COLLECTION_NAME)
if not collection.has_index():
    print("No index found, creating one...")
    collection.create_index(
        field_name="vector",
        index_params={
            "index_type": "IVF_FLAT",
            "metric_type": "IP",
            "params": {"nlist": NLIST}, 
        }
    )
else:
    print("Index already exists.")

collection.load()

device = "cuda" if torch.cuda.is_available() else "cpu"
model  = CLIPModel.from_pretrained("openai/clip-vit-large-patch14").to(device)
proc   = CLIPProcessor.from_pretrained("openai/clip-vit-large-patch14")

_write_lock = Lock()

def embed(img: Image.Image) -> np.ndarray:
    t = proc(images=img, return_tensors="pt").to(device)
    with torch.no_grad():
        v = model.get_image_features(**t).cpu().numpy().astype("float32")
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
