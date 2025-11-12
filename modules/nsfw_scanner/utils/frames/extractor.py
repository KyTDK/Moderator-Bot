from __future__ import annotations

import os
from threading import Event
from typing import Iterator, Optional

from .ffmpeg import ffmpeg_available, iter_video_frames_ffmpeg
from .models import ExtractedFrame
from .opencv import iter_video_frames_opencv
from .pillow_iterators import iter_gif_frames, iter_pillow_frames


def iter_extracted_frames(
    filename: str,
    wanted: Optional[int],
    *,
    use_hwaccel: bool = False,
    accelerated_tier: bool | None = None,
    stop_event: Event | None = None,
) -> Iterator[ExtractedFrame]:
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".gif":
        yield from iter_gif_frames(filename, wanted, stop_event=stop_event)
        return
    if ext in {".webp", ".apng", ".avif"}:
        yield from iter_pillow_frames(filename, wanted, stop_event=stop_event)
        return

    if ffmpeg_available():
        try:
            ffmpeg_yielded = False
            for payload in iter_video_frames_ffmpeg(
                filename,
                wanted,
                stop_event=stop_event,
            ):
                ffmpeg_yielded = True
                yield payload
                if stop_event and stop_event.is_set():
                    return
            if ffmpeg_yielded:
                return
        except Exception as exc:
            print(f"[iter_extracted_frames] ffmpeg pipeline failed on {filename}: {exc}")

    yield from iter_video_frames_opencv(
        filename,
        wanted,
        use_hwaccel=use_hwaccel,
        accelerated_tier=accelerated_tier,
        stop_event=stop_event,
    )
