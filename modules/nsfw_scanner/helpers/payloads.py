from __future__ import annotations

import asyncio
import io
import os
from dataclasses import dataclass

from PIL import Image


__all__ = [
    "PreparedImagePayload",
    "flatten_alpha",
    "prepare_image_payload_sync",
    "prepare_image_payload",
    "MAX_IMAGE_EDGE",
    "INLINE_PASSTHROUGH_BYTES",
    "JPEG_TARGET_BYTES",
    "JPEG_INITIAL_QUALITY",
    "JPEG_MIN_QUALITY",
    "VIDEO_FRAME_MAX_EDGE",
    "VIDEO_FRAME_TARGET_BYTES",
]


_RESAMPLING_FILTER = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.BICUBIC)

MAX_IMAGE_EDGE = int(os.getenv("MODBOT_MODERATION_MAX_IMAGE_EDGE", "1536"))
INLINE_PASSTHROUGH_BYTES = int(os.getenv("MODBOT_MODERATION_INLINE_THRESHOLD", "262144"))
JPEG_TARGET_BYTES = int(os.getenv("MODBOT_MODERATION_TARGET_BYTES", "1250000"))
JPEG_INITIAL_QUALITY = int(os.getenv("MODBOT_MODERATION_JPEG_QUALITY", "82"))
JPEG_MIN_QUALITY = int(os.getenv("MODBOT_MODERATION_MIN_JPEG_QUALITY", "58"))
VIDEO_FRAME_MAX_EDGE = int(os.getenv("MODBOT_MODERATION_VIDEO_MAX_EDGE", "768"))
VIDEO_FRAME_TARGET_BYTES = int(os.getenv("MODBOT_MODERATION_VIDEO_TARGET_BYTES", "350000"))


@dataclass(slots=True)
class PreparedImagePayload:
    data: bytes
    mime: str
    width: int
    height: int
    resized: bool
    strategy: str
    quality: int | None
    original_mime: str | None


def flatten_alpha(image: Image.Image) -> Image.Image:
    """Return an RGB image with alpha composited on white."""
    if image.mode not in {"RGBA", "LA"}:
        return image.convert("RGB") if image.mode != "RGB" else image
    base = Image.new("RGB", image.size, (255, 255, 255))
    rgb = image.convert("RGB")
    alpha = image.split()[-1]
    base.paste(rgb, mask=alpha)
    return base


def prepare_image_payload_sync(
    *,
    image: Image.Image | None,
    image_bytes: bytes | None,
    image_path: str | None,
    image_mime: str | None,
    original_size: int | None,
    max_image_edge: int | None = None,
    jpeg_target_bytes: int | None = None,
) -> PreparedImagePayload:
    working: Image.Image | None = None
    close_working = False
    original_mime = (image_mime or "").lower() or None

    edge_limit = int(max_image_edge) if max_image_edge else MAX_IMAGE_EDGE
    if edge_limit <= 0:
        edge_limit = MAX_IMAGE_EDGE
    target_bytes = int(jpeg_target_bytes) if jpeg_target_bytes else JPEG_TARGET_BYTES
    if target_bytes <= 0:
        target_bytes = JPEG_TARGET_BYTES

    if image is not None:
        try:
            working = image.copy()
            working.load()
            close_working = True
        except Exception:
            working = None

    if working is None:
        if image_bytes is not None:
            stream = io.BytesIO(image_bytes)
            loaded = Image.open(stream)
            loaded.load()
            working = loaded
            close_working = True
        elif image_path is not None and os.path.exists(image_path):
            loaded = Image.open(image_path)
            loaded.load()
            working = loaded
            close_working = True
        else:
            raise ValueError("No image data available for moderation payload preparation")

    try:
        width, height = working.size
    except Exception as exc:
        if close_working and working is not None:
            working.close()
        raise RuntimeError("Failed to read image dimensions") from exc

    passthrough_allowed = (
        original_size is not None
        and original_size <= INLINE_PASSTHROUGH_BYTES
        and max(width, height) <= edge_limit
        and original_mime in {"image/jpeg", "image/jpg"}
        and image_bytes is not None
    )
    if passthrough_allowed:
        data_bytes = image_bytes
        if data_bytes is None and image_path and os.path.exists(image_path):
            with open(image_path, "rb") as file_obj:
                data_bytes = file_obj.read()
        payload = PreparedImagePayload(
            data=data_bytes or b"",
            mime=image_mime or "image/jpeg",
            width=width,
            height=height,
            resized=False,
            strategy="passthrough",
            quality=None,
            original_mime=image_mime,
        )
        if close_working and working is not None:
            working.close()
        return payload

    resized = False
    max_edge = max(width, height)
    if max_edge > edge_limit and working.size[0] > 0 and working.size[1] > 0:
        scale = edge_limit / float(max_edge)
        new_size = (
            max(1, int(round(working.size[0] * scale))),
            max(1, int(round(working.size[1] * scale))),
        )
        working = working.resize(new_size, _RESAMPLING_FILTER)
        width, height = working.size
        resized = True

    working = flatten_alpha(working)

    buffer = io.BytesIO()
    prepared = working
    try:
        if prepared.mode != "RGB":
            prepared = prepared.convert("RGB")

        qualities = [
            q for q in range(JPEG_INITIAL_QUALITY, JPEG_MIN_QUALITY - 1, -10)
        ]
        if qualities[-1] != JPEG_MIN_QUALITY:
            qualities.append(JPEG_MIN_QUALITY)
        final_bytes: bytes | None = None
        chosen_quality = JPEG_MIN_QUALITY

        for quality in qualities:
            try:
                buffer.seek(0)
                buffer.truncate(0)
                prepared.save(
                    buffer,
                    format="JPEG",
                    quality=max(10, min(95, quality)),
                    optimize=True,
                    progressive=True,
                )
            except OSError:
                buffer.seek(0)
                buffer.truncate(0)
                prepared.convert("RGB").save(
                    buffer,
                    format="JPEG",
                    quality=max(10, min(95, quality)),
                )
            data = buffer.getvalue()
            final_bytes = data
            chosen_quality = quality
            if len(data) <= target_bytes:
                break

        if final_bytes is None:
            raise RuntimeError("Failed to encode moderation payload")

        payload = PreparedImagePayload(
            data=final_bytes,
            mime="image/jpeg",
            width=width,
            height=height,
            resized=resized,
            strategy="compressed_jpeg",
            quality=chosen_quality,
            original_mime=image_mime,
        )
        return payload
    finally:
        if close_working and working is not None and working is not prepared:
            working.close()
        if prepared is not working and prepared is not None:
            prepared.close()
        buffer.close()


def prepare_image_payload(
    *,
    image: Image.Image | None,
    image_bytes: bytes | None,
    image_path: str | None,
    image_mime: str | None,
    original_size: int | None,
    max_image_edge: int | None = None,
    jpeg_target_bytes: int | None = None,
) -> asyncio.Future[PreparedImagePayload]:
    return asyncio.to_thread(
        prepare_image_payload_sync,
        image=image,
        image_bytes=image_bytes,
        image_path=image_path,
        image_mime=image_mime,
        original_size=original_size,
        max_image_edge=max_image_edge,
        jpeg_target_bytes=jpeg_target_bytes,
    )
