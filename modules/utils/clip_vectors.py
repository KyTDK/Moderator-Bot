from __future__ import annotations

import logging
import os
from threading import Lock
from typing import Any, Iterable, List, Sequence

import numpy as np

try:
    import torch
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    torch = None

from PIL import Image
from dotenv import load_dotenv

try:
    from transformers import CLIPModel, CLIPProcessor
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    CLIPModel = None
    CLIPProcessor = None

from .vector_spaces import MilvusVectorSpace, VectorDeleteStats

load_dotenv()

# Milvus connection/config
MILVUS_HOST = os.getenv("MILVUS_HOST", "localhost")
MILVUS_PORT = os.getenv("MILVUS_PORT", "19530")
COLLECTION_NAME = "clip_vectors"

log = logging.getLogger(__name__)

_model = None
_proc = None
_device = None
_init_lock = Lock()
_preferred_device = "cuda" if torch is not None and torch.cuda.is_available() else "cpu"

if torch is not None and _preferred_device == "cuda":
    _device_details = torch.cuda.get_device_name(torch.cuda.current_device())
    _startup_message = f"CLIP vectors configured to use CUDA device: {_device_details}"
elif torch is not None:
    _startup_message = "CLIP vectors running on CPU; CUDA device not detected"
else:
    _startup_message = "CLIP vectors disabled: PyTorch not installed"

log.info(_startup_message)
print(_startup_message)


def _ensure_clip_loaded() -> None:
    """Load CLIP model/processor on first use (thread-safe)."""

    global _model, _proc, _device
    if _model is not None and _proc is not None and _device is not None:
        return

    if torch is None or CLIPModel is None or CLIPProcessor is None:
        raise RuntimeError(
            "CLIP vector support requires the optional 'torch' and 'transformers' dependencies."
        )

    # Older torch builds lack torch.Lock, so fall back to multiprocessing lock
    with _init_lock:
        if _model is not None and _proc is not None and _device is not None:
            return
        dev = _preferred_device
        model = CLIPModel.from_pretrained("openai/clip-vit-large-patch14")
        processor = CLIPProcessor.from_pretrained("openai/clip-vit-large-patch14")
        _model = model.to(dev).eval()
        _proc = processor
        _device = dev


try:
    _RESAMPLE_NEAREST = Image.Resampling.NEAREST  # Pillow >= 9
except AttributeError:  # pragma: no cover - compatibility fallback
    _RESAMPLE_NEAREST = Image.NEAREST


def _prepare_clip_image(image: Image.Image) -> Image.Image:
    """Normalize image mode/size to avoid HuggingFace channel inference issues."""

    if not isinstance(image, Image.Image):
        raise TypeError(f"Expected PIL.Image.Image, received {type(image)!r}")

    prepared = image
    if prepared.mode != "RGB":
        # Drop alpha/other modes to ensure 3-channel tensors for CLIP.
        prepared = prepared.convert("RGB")

    width, height = prepared.size
    new_width = max(2, width)
    new_height = max(2, height)
    if new_width != width or new_height != height:
        prepared = prepared.resize((new_width, new_height), resample=_RESAMPLE_NEAREST)

    return prepared


def embed_batch(images: Sequence[Image.Image]) -> np.ndarray:
    """Return L2-normalised embeddings for a batch of images."""

    if not images:
        return np.empty((0, 0), dtype="float32")

    if torch is None:
        raise RuntimeError(
            "CLIP vector support is unavailable because PyTorch is not installed."
        )

    _ensure_clip_loaded()
    images = list(images)
    prepared_images = [_prepare_clip_image(image) for image in images]
    processed = _proc(images=prepared_images, return_tensors="pt")
    processed = processed.to(_device) if hasattr(processed, "to") else processed

    with torch.no_grad():
        vectors = _model.get_image_features(**processed).cpu().numpy().astype("float32")

    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    vectors = vectors / norms
    return vectors


def embed(image: Image.Image) -> np.ndarray:
    """Return a single image embedding (legacy helper)."""

    return embed_batch([image])


_IMAGE_VECTOR_SPACE = MilvusVectorSpace(
    collection_name=COLLECTION_NAME,
    dim=768,
    embed_batch=embed_batch,
    description="CLIP embeddings for content moderation",
    metric_type="IP",
    host=MILVUS_HOST,
    port=MILVUS_PORT,
    logger=log,
)


def register_failure_callback(callback) -> None:
    _IMAGE_VECTOR_SPACE.register_failure_callback(callback)


def is_available() -> bool:
    return _IMAGE_VECTOR_SPACE.is_available()


def is_fallback_active() -> bool:
    return _IMAGE_VECTOR_SPACE.is_fallback_active()


def get_last_error() -> Exception | None:
    return _IMAGE_VECTOR_SPACE.get_last_error()


def add_vector(image: Image.Image, metadata: dict[str, Any]) -> int | None:
    return _IMAGE_VECTOR_SPACE.add_vector(image, metadata)


async def delete_vectors(ids: Iterable[int]) -> VectorDeleteStats | None:
    return await _IMAGE_VECTOR_SPACE.delete_vectors(ids)


def query_similar(
    image: Image.Image,
    threshold: float = 0.80,
    k: int = 20,
    min_votes: int = 1,
) -> List[dict[str, Any]]:
    return _IMAGE_VECTOR_SPACE.query_similar(
        image,
        threshold=threshold,
        k=k,
        min_votes=min_votes,
    )


def query_similar_batch(
    images: Sequence[Image.Image],
    threshold: float = 0.80,
    k: int = 20,
    min_votes: int = 1,
) -> List[List[dict[str, Any]]]:
    return _IMAGE_VECTOR_SPACE.query_similar_batch(
        images,
        threshold=threshold,
        k=k,
        min_votes=min_votes,
    )


__all__ = [
    "COLLECTION_NAME",
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
    "VectorDeleteStats",
]
