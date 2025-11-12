from __future__ import annotations

import os
from typing import Iterable, Optional

import cv2
import numpy as np

from .config import PREVIEW_MAX_DIMENSION


def adaptive_cap(
    filename: str,
    fallback_limit: Optional[int],
    total_frames: int,
    duration_seconds: float | None,
) -> int:
    try:
        file_size = os.path.getsize(filename)
    except OSError:
        file_size = 0

    size_mb = file_size / (1024 * 1024)
    dynamic_cap = int(min(max(5, 5 + size_mb * 6), 60))

    if duration_seconds and duration_seconds > 0:
        duration_cap = int(min(60, max(5, duration_seconds * 6)))
        dynamic_cap = max(dynamic_cap, duration_cap)

    if total_frames > 0:
        dynamic_cap = min(dynamic_cap, total_frames)

    if fallback_limit is None or fallback_limit <= 0:
        return max(1, dynamic_cap)
    return max(1, min(fallback_limit, dynamic_cap))


def build_preview_frame(frame_rgb: np.ndarray) -> np.ndarray:
    if frame_rgb is None:
        return np.zeros((1, 1), dtype="float32")
    height, width = frame_rgb.shape[:2]
    longest = max(height, width)
    if longest > PREVIEW_MAX_DIMENSION and longest > 0:
        scale = PREVIEW_MAX_DIMENSION / float(longest)
        frame_rgb = cv2.resize(
            frame_rgb,
            (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
            interpolation=cv2.INTER_AREA,
        )
    preview = frame_rgb
    try:
        preview = cv2.cvtColor(preview, cv2.COLOR_RGB2GRAY)
    except Exception:
        try:
            preview = cv2.cvtColor(preview, cv2.COLOR_BGR2GRAY)
        except Exception:
            preview = preview[:, :, 0] if preview.ndim == 3 else preview
    return preview.astype("float32") / 255.0


def motion_scores(preview_frames: list[np.ndarray]) -> list[float]:
    if not preview_frames:
        return []
    scores: list[float] = [0.0]
    previous = preview_frames[0]
    try:
        prev_hist = cv2.calcHist([previous], [0], None, [16], [0, 1])
        prev_hist = cv2.normalize(prev_hist, prev_hist).flatten()
    except Exception:
        prev_hist = np.zeros(16, dtype="float32")
    for preview in preview_frames[1:]:
        diff = np.mean(np.abs(preview - previous))
        try:
            hist = cv2.calcHist([preview], [0], None, [16], [0, 1])
            hist = cv2.normalize(hist, hist).flatten()
        except Exception:
            hist = np.zeros_like(prev_hist)
        hist_delta = float(np.linalg.norm(hist - prev_hist, ord=1))
        score = float(diff * 0.7 + hist_delta * 0.3)
        scores.append(score)
        previous = preview
        prev_hist = hist
    return scores


def select_motion_keyframes(total_frames: int, cap: int, scores: list[float]) -> list[int]:
    if total_frames <= 0 or cap <= 0:
        return []
    if cap >= total_frames:
        return list(range(total_frames))

    selected: set[int] = set()
    base_samples = min(total_frames, max(1, min(cap, 5)))
    if base_samples == 1:
        selected.add(total_frames // 2)
    else:
        step = (total_frames - 1) / float(base_samples - 1)
        for i in range(base_samples):
            idx = int(round(i * step))
            selected.add(min(total_frames - 1, max(0, idx)))

    if scores:
        sorted_candidates = sorted(range(total_frames), key=lambda idx: scores[idx], reverse=True)
        median_score = float(np.median(scores)) if scores else 0.0
        motion_threshold = max(median_score * 1.4, 0.02)
        for idx in sorted_candidates:
            if len(selected) >= cap:
                break
            if scores[idx] < motion_threshold:
                continue
            selected.add(idx)
            if len(selected) >= cap:
                break
            for neighbor in (idx - 1, idx + 1):
                if len(selected) >= cap:
                    break
                if 0 <= neighbor < total_frames and neighbor not in selected:
                    selected.add(neighbor)
    ordered = sorted(selected)
    return ordered[:cap]
