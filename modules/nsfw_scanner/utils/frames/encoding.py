from __future__ import annotations

import logging
import uuid
from typing import Optional

import numpy as np

try:
    import cv2
except Exception as exc:  # pragma: no cover - optional dependency
    cv2 = None  # type: ignore
    _cv2_import_error = exc
else:
    _cv2_import_error = None

log = logging.getLogger(__name__)

from .config import (
    DEDUP_SIGNATURE_DIM,
    FRAME_MAX_INLINE_BYTES,
    FRAME_MIN_EDGE,
    TARGET_MAX_DIMENSION,
)
from .models import ExtractedFrame


def resize_for_model(frame_rgb: np.ndarray) -> np.ndarray:
    if cv2 is None:
        return frame_rgb
    if frame_rgb is None:
        return frame_rgb
    height, width = frame_rgb.shape[:2]
    if height == 0 or width == 0:
        return frame_rgb
    longest_edge = max(height, width)
    if longest_edge <= TARGET_MAX_DIMENSION:
        return frame_rgb
    scale = TARGET_MAX_DIMENSION / float(longest_edge)
    new_width = max(1, int(round(width * scale)))
    new_height = max(1, int(round(height * scale)))
    return cv2.resize(frame_rgb, (new_width, new_height), interpolation=cv2.INTER_AREA)


def compute_signature_from_rgb(frame_rgb: np.ndarray) -> Optional[np.ndarray]:
    if cv2 is None:
        log.debug("OpenCV unavailable; cannot compute frame signature")
        return None
    if frame_rgb is None:
        return None
    try:
        gray = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2GRAY)
    except Exception:
        try:
            gray = cv2.cvtColor(frame_rgb, cv2.COLOR_BGR2GRAY)
        except Exception:
            return None
    try:
        resized = cv2.resize(
            gray,
            (DEDUP_SIGNATURE_DIM, DEDUP_SIGNATURE_DIM),
            interpolation=cv2.INTER_AREA,
        )
    except Exception:
        return None
    return (resized.astype("float32") / 255.0).reshape(-1)


def encode_rgb_frame(
    frame_rgb: np.ndarray,
    frame_idx: int,
    total_frames: Optional[int],
) -> Optional[ExtractedFrame]:
    if cv2 is None:
        log.debug("OpenCV unavailable; skipping RGB frame encoding")
        return None
    if frame_rgb is None:
        return None
    working = frame_rgb
    max_bytes = FRAME_MAX_INLINE_BYTES if FRAME_MAX_INLINE_BYTES > 0 else None
    min_edge = max(1, FRAME_MIN_EDGE)
    qualities = [90, 88, 86, 82, 78, 74]
    data: bytes | None = None

    while True:
        encoded = False
        for quality in qualities:
            try:
                bgr_frame = cv2.cvtColor(working, cv2.COLOR_RGB2BGR)
            except Exception:
                bgr_frame = working
            success, buffer = cv2.imencode(
                ".jpg",
                bgr_frame,
                [cv2.IMWRITE_JPEG_QUALITY, quality],
            )
            if not success:
                continue
            payload = buffer.tobytes()
            encoded = True
            data = payload
            if max_bytes is None or len(payload) <= max_bytes or quality == qualities[-1]:
                break
        if not encoded or data is None:
            return None
        if max_bytes is None or len(data) <= max_bytes:
            break
        height, width = working.shape[:2]
        longest = max(height, width)
        if longest <= min_edge:
            break
        scale_ratio = max_bytes / float(len(data))
        scale_ratio = max(min(scale_ratio ** 0.5, 0.95), 0.5)
        new_width = max(1, int(round(width * scale_ratio)))
        new_height = max(1, int(round(height * scale_ratio)))
        if new_width < min_edge and new_height < min_edge:
            break
        try:
            working = cv2.resize(
                working,
                (new_width, new_height),
                interpolation=cv2.INTER_AREA,
            )
        except Exception:
            break

    resized_for_signature = resize_for_model(working)
    signature = compute_signature_from_rgb(resized_for_signature)
    if signature is None:
        return None

    return ExtractedFrame(
        name=f"{uuid.uuid4().hex[:8]}_{frame_idx}.jpg",
        data=data,
        mime_type="image/jpeg",
        signature=signature,
        total_frames=total_frames,
    )


def package_bgr_frame(
    frame_bgr: np.ndarray,
    frame_idx: int,
    total_frames: Optional[int],
) -> Optional[ExtractedFrame]:
    if cv2 is None:
        log.debug("OpenCV unavailable; cannot package BGR frame")
        return None
    if frame_bgr is None:
        return None
    try:
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    except Exception:
        rgb = frame_bgr
    return encode_rgb_frame(rgb, frame_idx, total_frames)


def frames_are_similar(
    signature_a: Optional[np.ndarray],
    signature_b: Optional[np.ndarray],
    *,
    threshold: float = 0.985,
) -> bool:
    if signature_a is None or signature_b is None:
        return False
    if signature_a.shape != signature_b.shape:
        return False
    diff = np.abs(signature_a - signature_b).mean()
    similarity = 1.0 - diff
    return similarity >= threshold
