from __future__ import annotations

import logging
import time
from typing import Optional

from modules.core.health import FeatureStatus, report_feature

from .utils.frames.ffmpeg import ffmpeg_available
from .utils.frames.opencv import detect_hwaccel_support

log = logging.getLogger(__name__)

__all__ = ["evaluate_video_capabilities", "revalidate_video_capabilities"]


def _build_metadata(source: Optional[str]) -> dict[str, object]:
    metadata: dict[str, object] = {"checked_at": time.time()}
    if source:
        metadata["source"] = source
    return metadata


def evaluate_video_capabilities(*, source: str | None = None) -> None:
    """
    Verify the state of the video extraction stack and publish results to the
    health registry so /stats shows actionable warnings.
    """

    metadata = _build_metadata(source)
    if ffmpeg_available():
        report_feature(
            "media.ffmpeg",
            label="FFmpeg video extraction",
            status=FeatureStatus.OK,
            category="media",
            detail="ffmpeg/ffprobe detected on PATH.",
            metadata=dict(metadata),
        )
    else:
        log.warning(
            "FFmpeg binaries are missing; video scans will fall back to slow OpenCV decoding."
        )
        report_feature(
            "media.ffmpeg",
            label="FFmpeg video extraction",
            status=FeatureStatus.UNAVAILABLE,
            category="media",
            detail="ffmpeg binary not found; falling back to OpenCV decode.",
            remedy="Install ffmpeg and ffprobe and ensure they are on PATH.",
            using_fallback=True,
            metadata=dict(metadata),
        )

    hw_supported, hw_detail = detect_hwaccel_support()
    if hw_supported:
        report_feature(
            "media.hw_decode",
            label="Hardware video decode",
            status=FeatureStatus.OK,
            category="media",
            detail=hw_detail or "Hardware acceleration available.",
            metadata=dict(metadata),
        )
    else:
        log.warning(
            "OpenCV hardware acceleration unavailable; falling back to CPU video decode: %s",
            hw_detail,
        )
        report_feature(
            "media.hw_decode",
            label="Hardware video decode",
            status=FeatureStatus.DEGRADED,
            category="media",
            detail=hw_detail or "Video decoding is CPU-only.",
            remedy="Provision GPU drivers / codecs or install CUDA-enabled OpenCV.",
            using_fallback=True,
            metadata=dict(metadata),
        )


def revalidate_video_capabilities(reason: str | None = None) -> None:
    """Convenience wrapper to force a re-check."""

    source = reason or "manual_revalidation"
    evaluate_video_capabilities(source=source)
