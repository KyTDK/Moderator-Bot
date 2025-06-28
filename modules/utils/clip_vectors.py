import os, json, pickle, numpy as np, faiss, torch
from PIL import Image
from collections import defaultdict
from torchvision import transforms
from transformers import CLIPProcessor, CLIPModel

DIM = 768
NLIST = 64  # Number of clusters for IVF index
MIN_TRAIN = max(32, NLIST * 40)
THRESHOLD = 0.70
K = 20
MIN_VOTES = 2

INDEX_PATH = "vector.index"
METADATA_PATH = "metadata.json"
PEND_VEC_PATH = "pending_vectors.pkl"
ALL_VECS_PATH = "all_vectors.npy"
ALL_META_PATH = "all_metadata.json"

device = "cuda" if torch.cuda.is_available() else "cpu"
model = CLIPModel.from_pretrained("openai/clip-vit-large-patch14").to(device)
proc = CLIPProcessor.from_pretrained("openai/clip-vit-large-patch14")
gpu_res = faiss.StandardGpuResources()

def _new_gpu_index() -> faiss.GpuIndexIVFFlat:
    cfg = faiss.GpuIndexIVFFlatConfig()
    cfg.device = 0
    cfg.indicesOptions = faiss.INDICES_64_BIT
    gpu_index = faiss.GpuIndexIVFFlat(
        gpu_res, DIM, NLIST, faiss.METRIC_INNER_PRODUCT, cfg
    )
    gpu_index.nprobe = max(1, NLIST // 4)
    return gpu_index

all_meta = json.load(open(ALL_META_PATH)) if os.path.exists(ALL_META_PATH) else []
stored_meta = json.load(open(METADATA_PATH)) if os.path.exists(METADATA_PATH) else []

index = _new_gpu_index()

def _train_and_add(vecs: np.ndarray):
    """(Re)train GPU index and add all vecs."""
    faiss.normalize_L2(vecs)
    index.reset()
    index.train(vecs)
    index.add(vecs)

if os.path.exists(INDEX_PATH) and not os.path.exists(ALL_VECS_PATH):
    print("[INFO] Extracting vectors from legacy CPU index …")
    cpu = faiss.read_index(INDEX_PATH)
    vecs = np.vstack([cpu.reconstruct(i) for i in range(cpu.ntotal)]).astype("float32")
    np.save(ALL_VECS_PATH, vecs)        
    _train_and_add(vecs)
    stored_meta = all_meta[:] 

elif os.path.exists(ALL_VECS_PATH):
    vecs = np.load(ALL_VECS_PATH).astype("float32")
    if len(vecs) >= MIN_TRAIN:
        _train_and_add(vecs)
        stored_meta = all_meta[:]

_pending = pickle.load(open(PEND_VEC_PATH, "rb")) if os.path.exists(PEND_VEC_PATH) else []

def _persist():
    """Save CPU copy + metadata."""
    faiss.write_index(faiss.index_gpu_to_cpu(index), INDEX_PATH)
    json.dump(stored_meta, open(METADATA_PATH, "w"), indent=2)

def _archive(vec: np.ndarray, meta: dict):
    v = np.load(ALL_VECS_PATH) if os.path.exists(ALL_VECS_PATH) else np.empty((0, DIM), 'float32')
    np.save(ALL_VECS_PATH, np.vstack([v, vec]))
    all_meta.append(meta)
    json.dump(all_meta, open(ALL_META_PATH, "w"), indent=2)

def embed(img: Image.Image) -> np.ndarray:
    t = proc(images=img, return_tensors="pt").to(device)
    with torch.no_grad():
        vec = model.get_image_features(**t).cpu().numpy().astype("float32")
    faiss.normalize_L2(vec)
    return vec

def _save_pending(): pickle.dump(_pending, open(PEND_VEC_PATH, "wb"))

def _maybe_train():
    global _pending
    if index.is_trained or len(_pending) < MIN_TRAIN:
        _save_pending(); return
    vecs = np.vstack(_pending).astype("float32")
    _train_and_add(vecs)
    _pending.clear(); os.remove(PEND_VEC_PATH); _persist()

def add_vector(img: Image.Image, metadata: dict):
    vec = embed(img)
    _archive(vec, metadata)
    if index.is_trained:
        index.add(vec)
    else:
        _pending.append(vec)
        _maybe_train()
    stored_meta.append(metadata)
    _persist()

def query_similar(img: Image.Image, threshold=THRESHOLD,
                  k=K, min_votes=MIN_VOTES) -> list[dict]:
    if not index.is_trained: return []
    vec = embed(img)
    D, I = index.search(vec, k)
    votes, hits = defaultdict(list), []
    for d, idx in zip(D[0], I[0]):
        if d < threshold or idx >= len(stored_meta): 
            continue
        meta = stored_meta[idx]; cat = meta.get("category")
        hits.append({**meta, "score": float(d)})
        votes[cat].append(d)
    if not votes: return []
    top_cat, scores = max(votes.items(), key=lambda x: len(x[1]))
    if len(scores) < min_votes: return []
    return [h for h in hits if h["category"] == top_cat]