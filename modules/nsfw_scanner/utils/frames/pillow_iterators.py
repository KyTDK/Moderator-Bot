from __future__ import annotations

from typing import Iterator, Optional

import numpy as np
from PIL import Image, ImageSequence

from .encoding import encode_rgb_frame
from .models import ExtractedFrame
from .sampling import adaptive_cap, build_preview_frame, motion_scores, select_motion_keyframes


def iter_gif_frames(
    filename: str,
    wanted: Optional[int],
    *,
    stop_event=None,
) -> Iterator[ExtractedFrame]:
    try:
        with Image.open(filename) as img:
            total_frames = getattr(img, "n_frames", 1) or 1
            if total_frames <= 0:
                return

            duration_ms = 0.0
            preview_frames: list[np.ndarray] = []
            for index, frame in enumerate(ImageSequence.Iterator(img)):
                if stop_event and stop_event.is_set():
                    return
                duration_ms += float(frame.info.get("duration", img.info.get("duration", 0)) or 0)
                rgb_frame = frame.convert("RGB")
                preview_frames.append(build_preview_frame(np.asarray(rgb_frame)))
                rgb_frame.close()
                if index + 1 >= total_frames:
                    break

            duration_seconds = duration_ms / 1000.0 if duration_ms else None
            target = adaptive_cap(filename, wanted, total_frames, duration_seconds)
            motion = motion_scores(preview_frames)
            indices = select_motion_keyframes(total_frames, target, motion)

            for idx in indices:
                if stop_event and stop_event.is_set():
                    return
                try:
                    img.seek(idx)
                except EOFError:
                    break
                frame = img.convert("RGB")
                payload = encode_rgb_frame(np.asarray(frame), idx, total_frames)
                frame.close()
                if payload is not None:
                    yield payload
    except Exception as exc:
        print(f"[iter_extracted_frames] Pillow GIF pipeline failed on {filename}: {exc}")


def iter_pillow_frames(
    filename: str,
    wanted: Optional[int],
    *,
    stop_event=None,
) -> Iterator[ExtractedFrame]:
    try:
        with Image.open(filename) as img:
            total_frames = getattr(img, "n_frames", 1)
            target = adaptive_cap(filename, wanted, total_frames, None)
            if target <= 0:
                return
            idxs = np.linspace(0, max(total_frames - 1, 0), target, dtype=int)
            for idx in idxs:
                if stop_event and stop_event.is_set():
                    return
                img.seek(int(idx))
                frame = img.convert("RGB")
                payload = encode_rgb_frame(
                    np.asarray(frame),
                    int(idx),
                    int(total_frames) if total_frames else None,
                )
                frame.close()
                if payload is not None:
                    yield payload
    except Exception as exc:
        print(f"[iter_extracted_frames] Pillow failed on {filename}: {exc}")
