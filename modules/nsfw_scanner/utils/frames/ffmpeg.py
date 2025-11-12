from __future__ import annotations

import io
import json
import shutil
import subprocess
from functools import lru_cache
from typing import Iterator, Optional

import numpy as np
from PIL import Image

from .config import FFMPEG_SCALE_EDGE, FFMPEG_TIMEOUT_SECONDS
from .encoding import encode_rgb_frame
from .models import ExtractedFrame
from .sampling import adaptive_cap


@lru_cache(maxsize=1)
def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _parse_fraction(value: str | None) -> float | None:
    if not value or value in {"0/0", "N/A"}:
        return None
    if "/" in value:
        numerator, denominator = value.split("/", 1)
        try:
            num = float(numerator)
            den = float(denominator)
            if den == 0:
                return None
            return num / den
        except (TypeError, ValueError):
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _probe_video_metadata(filename: str) -> tuple[int | None, float | None, float | None]:
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=nb_frames,r_frame_rate,avg_frame_rate,duration",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                filename,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
        )
    except (subprocess.SubprocessError, FileNotFoundError, TimeoutError, OSError):
        return None, None, None

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None, None, None

    streams = payload.get("streams") or []
    stream_info = streams[0] if streams else {}
    format_info = payload.get("format") or {}

    nb_frames_raw = stream_info.get("nb_frames")
    total_frames: int | None
    try:
        total_frames = int(nb_frames_raw) if nb_frames_raw not in {None, "N/A"} else None
    except (TypeError, ValueError):
        total_frames = None

    frame_rate = (
        _parse_fraction(stream_info.get("avg_frame_rate"))
        or _parse_fraction(stream_info.get("r_frame_rate"))
    )

    duration_raw = stream_info.get("duration") or format_info.get("duration")
    try:
        duration = float(duration_raw) if duration_raw not in {None, "N/A"} else None
    except (TypeError, ValueError):
        duration = None

    if total_frames is None and frame_rate and duration:
        total_frames = max(0, int(round(frame_rate * duration)))

    return total_frames, frame_rate, duration


def _iter_mjpeg_frames_from_pipe(stream: subprocess.Popen) -> Iterator[bytes]:
    if stream.stdout is None:
        return
    buffer = bytearray()
    while True:
        chunk = stream.stdout.read(4096)
        if not chunk:
            break
        buffer.extend(chunk)
        while True:
            start = buffer.find(b"\xff\xd8")
            if start == -1:
                if len(buffer) > 2:
                    del buffer[:-2]
                break
            end = buffer.find(b"\xff\xd9", start + 2)
            if end == -1:
                if start > 0:
                    del buffer[:start]
                break
            frame_bytes = bytes(buffer[start : end + 2])
            del buffer[: end + 2]
            yield frame_bytes


def _build_frame_from_bytes(
    frame_bytes: bytes,
    frame_idx: int,
    total_frames: Optional[int],
) -> Optional[ExtractedFrame]:
    try:
        with Image.open(io.BytesIO(frame_bytes)) as image:
            rgb_image = image.convert("RGB")
            frame_rgb = np.asarray(rgb_image)
    except Exception:
        return None
    return encode_rgb_frame(frame_rgb, frame_idx, total_frames)


def iter_video_frames_ffmpeg(
    filename: str,
    wanted: Optional[int],
    *,
    stop_event=None,
) -> Iterator[ExtractedFrame]:
    total_frames, frame_rate, duration = _probe_video_metadata(filename)
    if total_frames is None and frame_rate is None and duration is None:
        raise RuntimeError("ffmpeg probe failed")

    target_total = total_frames if total_frames is not None else 0
    target = adaptive_cap(filename, wanted, target_total, duration)
    if target <= 0:
        return

    if total_frames and total_frames > 0:
        idxs = np.linspace(0, max(total_frames - 1, 0), target, dtype=int)
    elif frame_rate and duration:
        approx_total = max(int(round(frame_rate * duration)), target)
        idxs = np.linspace(0, max(approx_total - 1, 0), target, dtype=int)
    else:
        idxs = np.arange(0, target, dtype=int)

    idxs_list = list(dict.fromkeys(int(idx) for idx in idxs))
    if not idxs_list:
        return

    select_terms = [f"eq(n\\,{idx})" for idx in idxs_list]
    select_expr = "+".join(select_terms)
    filters = [f"select={select_expr}"]
    if FFMPEG_SCALE_EDGE > 0:
        filters.append(
            f"scale='min({FFMPEG_SCALE_EDGE},iw)':'min({FFMPEG_SCALE_EDGE},ih)':force_original_aspect_ratio=decrease"
        )
    filter_arg = ",".join(filters)

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        filename,
        "-vf",
        filter_arg,
        "-vsync",
        "0",
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "-qscale:v",
        "3",
        "pipe:1",
    ]

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )

    try:
        frame_iter = _iter_mjpeg_frames_from_pipe(process)
        for frame_idx, frame_bytes in zip(idxs_list, frame_iter):
            if stop_event and stop_event.is_set():
                break
            extracted = _build_frame_from_bytes(
                frame_bytes,
                frame_idx,
                total_frames,
            )
            if extracted is not None:
                yield extracted
        if stop_event and process.poll() is None:
            process.terminate()
    finally:
        if process.stdout is not None:
            try:
                process.stdout.close()
            except Exception:
                pass
        try:
            process.wait(timeout=1)
        except Exception:
            process.kill()
            try:
                process.wait(timeout=1)
            except Exception:
                pass
