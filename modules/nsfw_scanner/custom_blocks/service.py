from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Mapping

from PIL import Image

from modules.config.premium_plans import (
    PLAN_CORE,
    PLAN_DISPLAY_NAMES,
    PLAN_FREE,
    PLAN_PRO,
    PLAN_ULTRA,
)
from modules.nsfw_scanner.helpers.image_io import _open_image_from_bytes
from modules.utils import clip_vectors
from modules.utils.mysql.premium import resolve_guild_plan

log = logging.getLogger(__name__)

CUSTOM_BLOCK_CATEGORY = "guild_custom_block"
_MAX_LABEL_LENGTH = 256
_CUSTOM_BLOCK_LIMITS: dict[str, int | None] = {
    PLAN_FREE: 0,
    PLAN_CORE: 250,
    PLAN_PRO: 1000,
    PLAN_ULTRA: None,
}
_DEFAULT_CUSTOM_BLOCK_LIMIT = 1000


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


async def _fetch_custom_block_rows() -> list[dict[str, Any]]:
    if not clip_vectors.is_available():
        return []

    def _list_raw() -> list[dict[str, Any]]:
        return clip_vectors.list_entries(category=CUSTOM_BLOCK_CATEGORY)

    return await asyncio.to_thread(_list_raw)


def _normalise_custom_block_entry(
    entry: Mapping[str, Any],
    guild_id: int,
) -> dict[str, Any] | None:
    meta_raw = entry.get("meta")
    if isinstance(meta_raw, str):
        try:
            meta = json.loads(meta_raw)
        except json.JSONDecodeError:
            meta = {}
    elif isinstance(meta_raw, Mapping):
        meta = dict(meta_raw)
    else:
        meta = {}

    try:
        meta_guild_id = int(meta.get("guild_id", -1))
    except (TypeError, ValueError):
        return None
    if meta_guild_id != int(guild_id):
        return None

    vector_id_value = entry.get("id", meta.get("vector_id"))
    try:
        vector_id = int(vector_id_value) if vector_id_value is not None else None
    except (TypeError, ValueError):
        vector_id = vector_id_value

    return {
        "vector_id": vector_id,
        "label": meta.get("label"),
        "uploaded_by": meta.get("uploaded_by"),
        "uploaded_at": meta.get("uploaded_at"),
        "source": meta.get("source"),
        "category": entry.get("category"),
        "metadata": meta,
    }


async def get_custom_block_count(guild_id: int, *, stop_after: int | None = None) -> int:
    rows = await _fetch_custom_block_rows()
    count = 0
    for entry in rows:
        if _normalise_custom_block_entry(entry, guild_id) is None:
            continue
        count += 1
        if stop_after is not None and count >= stop_after:
            return count
    return count


async def _enforce_custom_block_limit(guild_id: int, plan: str) -> None:
    limit = _CUSTOM_BLOCK_LIMITS.get(plan, _DEFAULT_CUSTOM_BLOCK_LIMIT)
    if limit is None:
        return
    plan_label = PLAN_DISPLAY_NAMES.get(plan, plan.title())
    if limit <= 0:
        raise CustomBlockError(
            f"The {plan_label} plan does not include custom image blocks."
        )

    current = await get_custom_block_count(guild_id, stop_after=limit)
    if current >= limit:
        raise CustomBlockError(
            f"The {plan_label} plan can store up to {limit} custom blocked images. "
            "Remove one before uploading another."
        )


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
        plan = await resolve_guild_plan(guild_id)
    except Exception as exc:  # pragma: no cover - defensive logging
        raise CustomBlockError("Failed to verify subscription status.") from exc

    if plan == PLAN_FREE:
        raise CustomBlockError("Accelerated plan required for custom image blocks.")

    await _enforce_custom_block_limit(guild_id, plan)

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


async def list_custom_blocks(guild_id: int) -> list[dict[str, Any]]:
    """Return metadata for all custom block vectors belonging to *guild_id*."""

    raw_entries = await _fetch_custom_block_rows()
    results: list[dict[str, Any]] = []
    for entry in raw_entries:
        normalized = _normalise_custom_block_entry(entry, guild_id)
        if normalized is None:
            continue
        results.append(normalized)

    results.sort(key=lambda item: item.get("uploaded_at") or 0, reverse=True)
    return results


async def delete_custom_block(guild_id: int, vector_id: int) -> dict[str, Any]:
    """Delete a custom block vector if it belongs to *guild_id*."""

    normalized_vector_id = _coerce_int(vector_id)
    if normalized_vector_id is None:
        raise CustomBlockError("vector_id is required for delete.")

    if not clip_vectors.is_available():
        raise CustomBlockError("Milvus vector store is unavailable.")

    expr = (
        f"id in [{normalized_vector_id}] and "
        f"category == {json.dumps(CUSTOM_BLOCK_CATEGORY)}"
    )
    raw = clip_vectors.list_entries(expr=expr)
    normalized = (
        _normalise_custom_block_entry(entry, guild_id)
        for entry in raw
    )
    match = next((entry for entry in normalized if entry is not None), None)
    if match is None:
        raise CustomBlockError("Vector does not exist for this guild.")

    stats = await clip_vectors.delete_vectors([normalized_vector_id])
    if stats is None:
        raise CustomBlockError("Milvus collection is not ready; delete aborted.")

    log.info(
        "Deleted custom image block",
        extra={
            "guild_id": guild_id,
            "vector_id": normalized_vector_id,
            "label": match.get("label"),
        },
    )
    return match


__all__ = [
    "CUSTOM_BLOCK_CATEGORY",
    "CustomBlockError",
    "add_custom_block_from_bytes",
    "list_custom_blocks",
    "delete_custom_block",
    "get_custom_block_count",
]
