import asyncio
import mimetypes
import os
import time
import traceback
from typing import Any, List, Optional

from PIL import Image

from modules.nsfw_scanner.settings_keys import NSFW_IMAGE_CATEGORY_SETTING

from ..utils.file_ops import safe_delete
from .context import ImageProcessingContext, build_image_processing_context
from .image_io import (
    _PNG_PASSTHROUGH_EXTS,
    _PNG_PASSTHROUGH_FORMATS,
    _encode_image_to_png_bytes,
    _is_truncated_image_error,
    _open_image_from_path,
)
from .image_logging import (
    _get_file_size,
    _notify_image_open_failure,
)
from .image_pipeline import _run_image_pipeline
from .metrics import LatencyTracker

NSFW_CATEGORY_SETTING = NSFW_IMAGE_CATEGORY_SETTING


async def process_image(
    scanner,
    original_filename: str,
    guild_id: int | None = None,
    clean_up: bool = True,
    settings: dict[str, Any] | None = None,
    accelerated: bool | None = None,
    *,
    convert_to_png: bool = True,
    context: ImageProcessingContext | None = None,
    similarity_response: Optional[List[dict[str, Any]]] = None,
    source_url: str | None = None,
    payload_metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    ctx = context
    if ctx is None:
        ctx = await build_image_processing_context(
            guild_id,
            settings=settings,
            accelerated=accelerated,
        )

    image: Image.Image | None = None
    latency_tracker = LatencyTracker()
    fallback_attempted = False
    recovery_error: Exception | None = None
    try:
        load_started = time.perf_counter()
        try:
            image = await _open_image_from_path(original_filename)
        except Exception as exc:
            if not _is_truncated_image_error(exc):
                raise
            fallback_attempted = True
            try:
                image = await _open_image_from_path(
                    original_filename,
                    allow_truncated=True,
                )
            except Exception as recovery_exc:
                recovery_error = recovery_exc
                raise exc from recovery_exc
        load_duration = max((time.perf_counter() - load_started) * 1000, 0.0)
        if load_duration > 0:
            latency_tracker.record_step(
                "image_open",
                load_duration,
                label="Open Image",
            )
        _, ext = os.path.splitext(original_filename)
        ext = ext.lower()
        image_info = getattr(image, "info", {}) if image is not None else {}
        original_format = str(image_info.get("original_format") or "").upper()
        passthrough = ext in _PNG_PASSTHROUGH_EXTS or (
            original_format and original_format in _PNG_PASSTHROUGH_FORMATS
        )
        needs_conversion = convert_to_png and not passthrough
        source_bytes = _get_file_size(original_filename)

        image_path: str | None = None if needs_conversion else original_filename
        image_bytes: bytes | None = None
        image_mime: str | None = None
        if not needs_conversion:
            image_mime = Image.MIME.get(original_format)
            if image_mime is None:
                guessed_mime, _ = mimetypes.guess_type(original_filename)
                image_mime = guessed_mime

        metadata: dict[str, Any] = dict(payload_metadata or {})
        if guild_id is not None:
            metadata.setdefault("guild_id", guild_id)
        metadata.setdefault("input_kind", "image")
        metadata["source_extension"] = ext or None
        metadata["original_format"] = original_format or None
        metadata["image_mode"] = getattr(image, "mode", None)
        metadata["image_size"] = list(image.size) if image else None
        metadata["conversion_performed"] = needs_conversion
        metadata.setdefault("payload_mime", None)
        metadata["passthrough"] = not needs_conversion
        if source_url and not metadata.get("source_url"):
            metadata["source_url"] = source_url
        metadata["source_bytes"] = source_bytes

        conversion_reason: str | None = None

        if needs_conversion:
            encode_started = time.perf_counter()
            image_bytes = await asyncio.to_thread(_encode_image_to_png_bytes, image)
            image_mime = "image/png"
            encode_duration = max((time.perf_counter() - encode_started) * 1000, 0.0)
            if encode_duration > 0:
                latency_tracker.record_step(
                    "image_encode",
                    encode_duration,
                    label="Encode PNG",
                )
            conversion_reason = "unsupported_format"
            metadata["payload_bytes"] = len(image_bytes or b"")
            metadata["payload_mime"] = image_mime
            metadata["conversion_target"] = "image/png"
            metadata["encode_duration_ms"] = encode_duration
        else:
            metadata["payload_mime"] = image_mime
            metadata["payload_bytes"] = metadata.get("source_bytes")
            metadata["conversion_target"] = None

        metadata["conversion_reason"] = conversion_reason

        response = await _run_image_pipeline(
            scanner,
            image_path=image_path,
            image=image,
            context=ctx,
            similarity_response=similarity_response,
            image_bytes=image_bytes,
            image_mime=image_mime,
            payload_metadata=metadata,
        )
        if isinstance(response, dict):
            pipeline_metrics = response.setdefault("pipeline_metrics", {})
            pipeline_metrics, _ = latency_tracker.merge_into_pipeline(pipeline_metrics)
            response["pipeline_metrics"] = pipeline_metrics
        return response
    except Exception as exc:
        print(traceback.format_exc())
        print(f"[process_image] Error processing image {original_filename}: {exc}")
        if image is None:
            _, failure_ext = os.path.splitext(original_filename)
            failure_metadata = {
                "guild_id": guild_id,
                "source_url": source_url,
                "source_bytes": _get_file_size(original_filename),
                "extension": (failure_ext or "").lower() or None,
                "conversion_requested": convert_to_png,
                "fallback_attempted": fallback_attempted,
                "fallback_mode": "LOAD_TRUNCATED_IMAGES" if fallback_attempted else None,
                "fallback_result": "Failed" if fallback_attempted else "Not Attempted",
            }
            if fallback_attempted and recovery_error is not None:
                failure_metadata["fallback_error"] = (
                    f"{type(recovery_error).__name__}: {recovery_error}"
                )
            await _notify_image_open_failure(
                scanner,
                filename=original_filename,
                exc=exc,
                metadata=failure_metadata,
            )
        return None
    finally:
        if image is not None:
            try:
                image.close()
            except Exception:
                pass
        if clean_up:
            safe_delete(original_filename)
