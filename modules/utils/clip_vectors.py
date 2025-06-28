import os, json, numpy as np, faiss, torch
from PIL import Image
from collections import defaultdict
from transformers import CLIPProcessor, CLIPModel
from threading import Lock

_write_lock = Lock()

DIM         = 768
NLIST       = 4
MIN_TRAIN   = max(32, NLIST * 40)
THRESHOLD   = 0.70
K           = 20
MIN_VOTES   = 2

INDEX_PATH      = "vector.index"
METADATA_PATH   = "metadata.json"
ALL_VECS_PATH   = "all_vectors.npy"
ALL_META_PATH   = "all_metadata.json"

device = "cuda" if torch.cuda.is_available() else "cpu"
model  = CLIPModel.from_pretrained("openai/clip-vit-large-patch14").to(device)
proc   = CLIPProcessor.from_pretrained("openai/clip-vit-large-patch14")
gpu_res = faiss.StandardGpuResources()

def _new_gpu_index() -> faiss.GpuIndexIVFFlat:
    cfg = faiss.GpuIndexIVFFlatConfig()
    cfg.device = 0
    cfg.indicesOptions = faiss.INDICES_64_BIT
    g = faiss.GpuIndexIVFFlat(gpu_res, DIM, NLIST, faiss.METRIC_INNER_PRODUCT, cfg)
    g.nprobe = max(1, NLIST // 4)
    return g

if os.path.exists(ALL_META_PATH):
    stored_meta = json.load(open(ALL_META_PATH))
elif os.path.exists(METADATA_PATH):
    stored_meta = json.load(open(METADATA_PATH))
else:
    stored_meta = []

index = _new_gpu_index()

def _train_and_add(vecs: np.ndarray):
    faiss.normalize_L2(vecs)
    index.reset()
    index.train(vecs)
    index.add(vecs)

if os.path.exists(INDEX_PATH) and not os.path.exists(ALL_VECS_PATH):
    print("[INFO] Extracting vectors from legacy CPU index …")
    cpu  = faiss.read_index(INDEX_PATH)
    vecs = np.vstack([cpu.reconstruct(i) for i in range(cpu.ntotal)]).astype("float32")
    np.save(ALL_VECS_PATH, vecs)
    _train_and_add(vecs)
elif os.path.exists(ALL_VECS_PATH):
    vecs = np.load(ALL_VECS_PATH).astype("float32")
    if len(vecs) >= MIN_TRAIN:
        _train_and_add(vecs)

if os.path.exists(ALL_VECS_PATH):
    vc = np.load(ALL_VECS_PATH).shape[0]
    if vc != len(stored_meta):
        raise RuntimeError(f"Startup mismatch: {vc} vectors vs {len(stored_meta)} meta rows.")

def _persist_meta():
    json.dump(stored_meta, open(METADATA_PATH,  "w"), indent=2)
    json.dump(stored_meta, open(ALL_META_PATH, "w"), indent=2)

def _persist():
    assert index.ntotal == len(stored_meta), (
        f"Persist mismatch: {index.ntotal} vectors vs {len(stored_meta)} metadata rows."
    )
    faiss.write_index(faiss.index_gpu_to_cpu(index), INDEX_PATH)
    _persist_meta()

def _maybe_train():
    if index.is_trained:
        return
    vecs = np.load(ALL_VECS_PATH).astype("float32")
    if len(vecs) < MIN_TRAIN:
        return
    _train_and_add(vecs)
    _persist()

def embed(img: Image.Image) -> np.ndarray:
    t = proc(images=img, return_tensors="pt").to(device)
    with torch.no_grad():
        v = model.get_image_features(**t).cpu().numpy().astype("float32")
    faiss.normalize_L2(v)
    return v

def add_vector(img: Image.Image, metadata: dict):
    vec = embed(img)

    with _write_lock:
        v = np.load(ALL_VECS_PATH) if os.path.exists(ALL_VECS_PATH) else np.empty((0, DIM), 'float32')
        np.save(ALL_VECS_PATH, np.vstack([v, vec]))

        stored_meta.append(metadata)

        if index.is_trained:
            index.add(vec)
            _persist()
        else:
            _persist_meta()
            _maybe_train()

def query_similar(img: Image.Image,
                  threshold: float = THRESHOLD,
                  k: int = K,
                  min_votes: int = MIN_VOTES) -> list[dict]:
    if not index.is_trained:
        return []
    vec = embed(img)
    D, I = index.search(vec, k)
    votes, hits = defaultdict(list), []
    for d, idx in zip(D[0], I[0]):
        if d < threshold or idx >= len(stored_meta):
            continue
        meta = stored_meta[idx]
        cat  = meta.get("category")
        hits.append({**meta, "score": float(d)})
        votes[cat].append(d)
    if not votes:
        return []
    top_cat, scores = max(votes.items(), key=lambda x: len(x[1]))
    if len(scores) < min_votes:
        return []
    return [h for h in hits if h["category"] == top_cat]