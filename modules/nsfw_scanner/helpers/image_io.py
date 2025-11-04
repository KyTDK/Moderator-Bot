import asyncio
import contextlib
import io
import os
import threading
from typing import Any

from PIL import Image, ImageFile

try:
    from pillow_heif import register_heif_opener
except ImportError:  # pragma: no cover - optional dependency handled gracefully
    register_heif_opener = None
else:  # pragma: no cover - exercised implicitly during module import
    register_heif_opener()

_PNG_PASSTHROUGH_EXTS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".jfif",
    ".webp",
}
_PNG_PASSTHROUGH_FORMATS = {
    "PNG",
    "JPEG",
    "JPG",
    "JFIF",
    "WEBP",
}

_TRUNCATED_ERROR_MARKERS: tuple[str, ...] = (
    "image file is truncated",
    "truncated file read",
    "unrecognized data stream contents when reading image file",
)
_TRUNCATED_LOAD_LOCK = threading.Lock()


@contextlib.contextmanager
def _temporary_truncated_loading(enabled: bool):
    if not enabled:
        yield
        return

    with _TRUNCATED_LOAD_LOCK:
        previous = ImageFile.LOAD_TRUNCATED_IMAGES
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        try:
            yield
        finally:
            ImageFile.LOAD_TRUNCATED_IMAGES = previous


def _prepare_loaded_image(image: Image.Image) -> Image.Image:
    try:
        image.load()
        original_format = (image.format or "").upper()
        if image.mode != "RGBA":
            converted = image.convert("RGBA")
            converted.load()
            if original_format:
                converted.info["original_format"] = original_format
            image.close()
            image = converted
        elif original_format:
            image.info["original_format"] = original_format
        return image
    except Exception:
        image.close()
        raise


def _is_truncated_image_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _TRUNCATED_ERROR_MARKERS)


async def _open_image_from_path(path: str, *, allow_truncated: bool = False) -> Image.Image:
    def _load() -> Image.Image:
        with _temporary_truncated_loading(allow_truncated):
            image = Image.open(path)
            return _prepare_loaded_image(image)

    return await asyncio.to_thread(_load)


async def _open_image_from_bytes(data: bytes) -> Image.Image:
    def _load() -> Image.Image:
        buffer = io.BytesIO(data)
        try:
            image = Image.open(buffer)
            return _prepare_loaded_image(image)
        finally:
            buffer.close()

    return await asyncio.to_thread(_load)


def _encode_image_to_png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    try:
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    finally:
        buffer.close()


__all__ = [
    "_PNG_PASSTHROUGH_EXTS",
    "_PNG_PASSTHROUGH_FORMATS",
    "_TRUNCATED_ERROR_MARKERS",
    "_temporary_truncated_loading",
    "_prepare_loaded_image",
    "_is_truncated_image_error",
    "_open_image_from_path",
    "_open_image_from_bytes",
    "_encode_image_to_png_bytes",
]
