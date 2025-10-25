from __future__ import annotations

import os
from typing import Optional, Tuple

import filetype
from PIL import Image

__all__ = [
    "ANIMATED_EXTS",
    "IMAGE_EXTS",
    "VIDEO_EXTS",
    "FILE_TYPE_IMAGE",
    "FILE_TYPE_VIDEO",
    "FILE_TYPE_UNKNOWN",
    "FILE_TYPE_LABELS",
    "determine_file_type",
]

ANIMATED_EXTS = {".gif", ".webp", ".apng", ".avif"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".heic"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}

FILE_TYPE_IMAGE = "image"
FILE_TYPE_VIDEO = "video"
FILE_TYPE_UNKNOWN = "unknown"

FILE_TYPE_LABELS = {
    FILE_TYPE_IMAGE: "Image",
    FILE_TYPE_VIDEO: "Video",
    FILE_TYPE_UNKNOWN: "Unknown",
}


def determine_file_type(filename: str) -> Tuple[str, Optional[str]]:
    """Detect whether a file is an image, a video, or something else."""
    ext = os.path.splitext(filename)[1].lower()
    kind = filetype.guess(filename)

    if not kind:
        if ext in ANIMATED_EXTS or ext in VIDEO_EXTS:
            return FILE_TYPE_VIDEO, None
        if ext in IMAGE_EXTS:
            return FILE_TYPE_IMAGE, None
        return FILE_TYPE_UNKNOWN, None

    mime = kind.mime or ""

    if mime.startswith("video"):
        return FILE_TYPE_VIDEO, mime

    if mime.startswith("image"):
        if ext in ANIMATED_EXTS:
            try:
                with Image.open(filename) as image:
                    if getattr(image, "is_animated", False) or getattr(image, "n_frames", 1) > 1:
                        return FILE_TYPE_VIDEO, mime
            except Exception:
                return FILE_TYPE_VIDEO, mime
        return FILE_TYPE_IMAGE, mime

    return FILE_TYPE_UNKNOWN, mime or None
