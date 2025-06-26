import os, json, faiss, torch, pickle, numpy as np
from PIL import Image
from collections import defaultdict
from torchvision import transforms
from transformers import CLIPProcessor, CLIPModel

VECTOR_DIM   = 768
NLIST        = 256
MIN_TRAIN    = 1_000
THRESHOLD    = 0.85
K_NEIGHBOURS = 20
MIN_VOTES    = 2

INDEX_PATH       = "vector.index"
METADATA_PATH    = "metadata.json"
PENDING_VEC_PATH = "pending_vectors.pkl"

device    = "cuda" if torch.cuda.is_available() else "cpu"
model     = CLIPModel.from_pretrained("openai/clip-vit-large-patch14").to(device)
processor = CLIPProcessor.from_pretrained("openai/clip-vit-large-patch14")

preprocess = transforms.Compose([transforms.Resize((224, 224)), transforms.ToTensor()])
gpu_res    = faiss.StandardGpuResources()

def _new_cpu_index() -> faiss.IndexIVFFlat:
    quantizer = faiss.IndexFlatIP(VECTOR_DIM)
    return faiss.IndexIVFFlat(quantizer, VECTOR_DIM, NLIST, faiss.METRIC_INNER_PRODUCT)

# Load or create index
if os.path.exists(INDEX_PATH):
    cpu_index = faiss.read_index(INDEX_PATH)
    index = faiss.index_cpu_to_gpu(gpu_res, 0, cpu_index)
else:
    index = faiss.index_cpu_to_gpu(gpu_res, 0, _new_cpu_index())

# Load metadata
if os.path.exists(METADATA_PATH):
    with open(METADATA_PATH, "r", encoding="utf-8") as f:
        stored_metadata: list[dict] = json.load(f)
else:
    stored_metadata: list[dict] = []

# Load pending vectors
if os.path.exists(PENDING_VEC_PATH):
    with open(PENDING_VEC_PATH, "rb") as f:
        _pending: list[np.ndarray] = pickle.load(f)
else:
    _pending: list[np.ndarray] = []

def embed_image(img: Image.Image) -> np.ndarray:
    """Return a unit-norm CLIP embedding shaped (1, VECTOR_DIM)."""
    inputs = processor(images=img, return_tensors="pt").to(device)
    with torch.no_grad():
        vec = model.get_image_features(**inputs).cpu().numpy().astype("float32")
    faiss.normalize_L2(vec)
    return vec

def _save_pending():
    """Save pending vectors to disk."""
    with open(PENDING_VEC_PATH, "wb") as f:
        pickle.dump(_pending, f)

def _maybe_train():
    """Train IVF when enough vectors are buffered."""
    global _pending, index
    if index.is_trained or len(_pending) < max(NLIST, MIN_TRAIN):
        _save_pending()
        return
    train_data = np.vstack(_pending)
    index.train(train_data)
    index.add(train_data)
    _pending.clear()
    os.remove(PENDING_VEC_PATH)
    _persist()

def add_vector(img: Image.Image, metadata: dict):
    """Embed & store image with associated metadata."""
    vec = embed_image(img)
    if index.is_trained:
        index.add(vec)
    else:
        _pending.append(vec)
        _maybe_train()
    stored_metadata.append(metadata)
    _persist()

def query_similar(img: Image.Image,
                  threshold: float = THRESHOLD,
                  k: int = K_NEIGHBOURS,
                  min_votes: int = MIN_VOTES) -> list[dict]:
    """Return neighbours whose category wins a vote."""
    if not index.is_trained:
        return []
    vec = embed_image(img)
    D, I = index.search(vec, k)
    cat_scores = defaultdict(list)
    hits = []
    for score, idx in zip(D[0], I[0]):
        if score < threshold or idx >= len(stored_metadata):
            continue
        meta = stored_metadata[idx]
        cat = meta.get("category")
        if cat:
            cat_scores[cat].append(score)
            hits.append({**meta, "score": float(score)})
    if not cat_scores:
        return []
    top_cat, votes = max(cat_scores.items(), key=lambda x: len(x[1]))
    if len(votes) < min_votes:
        return []
    return [h for h in hits if h["category"] == top_cat]

def _persist():
    """Save CPU copy of index + metadata."""
    cpu_idx = faiss.index_gpu_to_cpu(index)
    faiss.write_index(cpu_idx, INDEX_PATH)
    with open(METADATA_PATH, "w", encoding="utf-8") as f:
        json.dump(stored_metadata, f, indent=2)