from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Mapping

from PIL import Image

from modules.nsfw_scanner.helpers.image_io import _open_image_from_bytes
from modules.utils import clip_vectors, mysql

log = logging.getLogger(__name__)

CUSTOM_BLOCK_CATEGORY = "guild_custom_block"
_MAX_LABEL_LENGTH = 256


class CustomBlockError(Exception):
    """Raised when the dashboard custom image block pipeline cannot persist data."""


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalise_label(label: str | None) -> str | None:
    if not label:
        return None
    cleaned = label.strip()
    if not cleaned:
        return None
    if len(cleaned) > _MAX_LABEL_LENGTH:
        return cleaned[:_MAX_LABEL_LENGTH].rstrip()
    return cleaned


async def add_custom_block_from_bytes(
    guild_id: int,
    data: bytes,
    *,
    uploaded_by: int | None = None,
    label: str | None = None,
    source: str | None = None,
    extra_metadata: Mapping[str, Any] | None = None,
) -> int:
    """Persist *data* as a Milvus vector restricted to *guild_id*."""

    if not data:
        raise CustomBlockError("Image payload was empty.")
    if not clip_vectors.is_available():
        raise CustomBlockError("Milvus vector store is unavailable.")

    try:
        accelerated = await mysql.is_accelerated(guild_id=guild_id)
    except Exception as exc:  # pragma: no cover - defensive logging
        raise CustomBlockError("Failed to verify subscription status.") from exc

    if not accelerated:
        raise CustomBlockError("Accelerated plan required for custom image blocks.")

    try:
        image = await _open_image_from_bytes(data)
    except Exception as exc:
        raise CustomBlockError("Unable to decode uploaded image.") from exc

    metadata: dict[str, Any] = {
        "category": CUSTOM_BLOCK_CATEGORY,
        "custom_block": True,
        "guild_id": int(guild_id),
        "uploaded_at": int(time.time()),
    }

    uploader_id = _coerce_int(uploaded_by)
    if uploader_id is not None:
        metadata["uploaded_by"] = uploader_id

    normalized_label = _normalise_label(label)
    if normalized_label:
        metadata["label"] = normalized_label

    if source:
        metadata["source"] = str(source)

    if extra_metadata:
        for key, value in extra_metadata.items():
            if key in metadata:
                continue
            metadata[key] = value

    def _insert_vector(image_obj: Image.Image, payload: Mapping[str, Any]) -> int | None:
        return clip_vectors.add_vector(image_obj, dict(payload))

    try:
        vector_id = await asyncio.to_thread(_insert_vector, image, metadata)
    except Exception as exc:
        raise CustomBlockError("Failed to insert vector into Milvus.") from exc
    finally:
        try:
            image.close()
        except Exception:  # pragma: no cover - cleanup best-effort
            pass

    if vector_id is None:
        raise CustomBlockError("Milvus did not return a vector id for the custom image.")

    try:
        numeric_vector_id = int(vector_id)
    except (TypeError, ValueError):
        numeric_vector_id = vector_id

    log.info(
        "Registered custom image block",
        extra={
            "guild_id": guild_id,
            "vector_id": numeric_vector_id,
            "label": metadata.get("label"),
        },
    )
    return numeric_vector_id


__all__ = [
    "CUSTOM_BLOCK_CATEGORY",
    "CustomBlockError",
    "add_custom_block_from_bytes",
]
