import os
import json
import faiss
import torch
import numpy as np
from PIL import Image
from torchvision import transforms
from transformers import CLIPProcessor, CLIPModel

# Constants
VECTOR_DIM = 768
INDEX_PATH = "vector.index"
METADATA_PATH = "metadata.json"

# Load model and processor
model = CLIPModel.from_pretrained("openai/clip-vit-large-patch14")
processor = CLIPProcessor.from_pretrained("openai/clip-vit-large-patch14")

# Normalize image before feeding into model
preprocess = transforms.Compose([
    transforms.Resize((224, 224)),
    transforms.ToTensor(),
])

# Initialize or load FAISS index
if os.path.exists(INDEX_PATH):
    index = faiss.read_index(INDEX_PATH)
else:
    index = faiss.IndexFlatIP(VECTOR_DIM)

# Load metadata from disk
if os.path.exists(METADATA_PATH):
    with open(METADATA_PATH, "r", encoding="utf-8") as f:
        stored_metadata = json.load(f)
else:
    stored_metadata = []

def embed_image(image: Image.Image) -> np.ndarray:
    """Convert PIL image to normalized CLIP embedding (np.ndarray shape=(1, 512))"""
    inputs = processor(images=image, return_tensors="pt")
    with torch.no_grad():
        image_features = model.get_image_features(**inputs)
    image_features = image_features / image_features.norm(p=2, dim=-1, keepdim=True)
    return image_features.cpu().numpy().astype("float32")


def add_vector(image: Image.Image, metadata: dict):
    """Add image vector to FAISS and metadata list"""
    vec = embed_image(image)
    index.add(vec)
    stored_metadata.append(metadata)
    _persist()


def query_similar(image: Image.Image, threshold=0.85, k=5) -> list[dict]:
    """Search for similar images above a cosine similarity threshold"""
    vec = embed_image(image)
    D, I = index.search(vec, k)
    results = []
    for score, idx in zip(D[0], I[0]):
        if score >= threshold and idx < len(stored_metadata):
            results.append({**stored_metadata[idx], "score": float(score)})
    return results


def _persist():
    """Persist both FAISS index and metadata to disk"""
    faiss.write_index(index, INDEX_PATH)
    with open(METADATA_PATH, "w", encoding="utf-8") as f:
        json.dump(stored_metadata, f, indent=2)