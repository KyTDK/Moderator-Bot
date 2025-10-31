from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from threading import Event, Lock
from typing import Iterator, Optional

import cv2
import numpy as np
from PIL import Image, ImageSequence

__all__ = [
    "ExtractedFrame",
    "iter_extracted_frames",
    "frames_are_similar",
]

PREVIEW_MAX_DIMENSION = 112
TARGET_MAX_DIMENSION = 224
DEDUP_SIGNATURE_DIM = 16

_FFMPEG_SCALE_EDGE = int(os.getenv("MODBOT_FFMPEG_MAX_EDGE", "640"))
_FFMPEG_TIMEOUT_SECONDS = float(os.getenv("MODBOT_FFMPEG_TIMEOUT", "45"))

_SUPPRESS_OPENCV_STDERR = os.environ.get("MODBOT_SUPPRESS_OPENCV_STDERR", "1").lower()
_SUPPRESS_OPENCV_STDERR = _SUPPRESS_OPENCV_STDERR not in {"0", "false", "off"}
_STDERR_REDIRECT_LOCK = Lock()


@contextmanager
def _suppress_cv2_stderr():
    if not _SUPPRESS_OPENCV_STDERR:
        yield
        return
    devnull_fd = None
    saved_stderr = None
    with _STDERR_REDIRECT_LOCK:
        try:
            saved_stderr = os.dup(2)
            devnull_fd = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull_fd, 2)
        except OSError:
            if saved_stderr is not None:
                try:
                    os.close(saved_stderr)
                except OSError:
                    pass
            if devnull_fd is not None:
                try:
                    os.close(devnull_fd)
                except OSError:
                    pass
            yield
            return
    try:
        yield
    finally:
        with _STDERR_REDIRECT_LOCK:
            if saved_stderr is not None:
                try:
                    os.dup2(saved_stderr, 2)
                finally:
                    os.close(saved_stderr)
            if devnull_fd is not None:
                try:
                    os.close(devnull_fd)
                except OSError:
                    pass


@dataclass(slots=True)
class ExtractedFrame:
    name: str
    data: bytes
    mime_type: str
    signature: Optional[np.ndarray]
    total_frames: Optional[int] = None


@lru_cache(maxsize=1)
def _ffmpeg_tools_available() -> bool:
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
    if not _ffmpeg_tools_available():
        return None, None, None
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
            timeout=_FFMPEG_TIMEOUT_SECONDS,
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


def _build_extracted_frame_from_bytes(
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
    return _encode_rgb_frame(frame_rgb, frame_idx, total_frames)


def _iter_video_frames_ffmpeg(
    filename: str,
    wanted: Optional[int],
    *,
    stop_event: Event | None = None,
) -> Iterator[ExtractedFrame]:
    total_frames, frame_rate, duration = _probe_video_metadata(filename)
    if total_frames is None and frame_rate is None and duration is None:
        raise RuntimeError("ffmpeg probe failed")

    target_total = total_frames if total_frames is not None else 0
    target = _adaptive_cap(filename, wanted, target_total, duration)
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
    filters = [
        f"select={select_expr}",
        f"scale='min({_FFMPEG_SCALE_EDGE},iw)':'min({_FFMPEG_SCALE_EDGE},ih)':force_original_aspect_ratio=decrease",
    ]
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
        frame_count = 0
        for frame_idx, frame_bytes in zip(idxs_list, frame_iter):
            if stop_event and stop_event.is_set():
                break
            extracted = _build_extracted_frame_from_bytes(
                frame_bytes,
                frame_idx,
                total_frames,
            )
            if extracted is not None:
                yield extracted
                frame_count += 1
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

def _adaptive_cap(
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


def _resize_for_model(frame_rgb: np.ndarray) -> np.ndarray:
    if frame_rgb is None:
        return frame_rgb
    height, width = frame_rgb.shape[:2]
    if height == 0 or width == 0:
        return frame_rgb
    longest_edge = max(height, width)
    if longest_edge <= TARGET_MAX_DIMENSION:
        return frame_rgb.copy()
    scale = TARGET_MAX_DIMENSION / float(longest_edge)
    new_width = max(1, int(round(width * scale)))
    new_height = max(1, int(round(height * scale)))
    return cv2.resize(frame_rgb, (new_width, new_height), interpolation=cv2.INTER_AREA)


def _compute_signature_from_rgb(frame_rgb: np.ndarray) -> Optional[np.ndarray]:
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
        resized = cv2.resize(gray, (DEDUP_SIGNATURE_DIM, DEDUP_SIGNATURE_DIM), interpolation=cv2.INTER_AREA)
    except Exception:
        return None
    return (resized.astype("float32") / 255.0).reshape(-1)


def _build_preview_frame(frame_rgb: np.ndarray) -> np.ndarray:
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


def _motion_scores(preview_frames: list[np.ndarray]) -> list[float]:
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


def _select_motion_keyframes(total_frames: int, cap: int, scores: list[float]) -> list[int]:
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


def _encode_rgb_frame(frame_rgb: np.ndarray, frame_idx: int, total_frames: Optional[int]) -> Optional[ExtractedFrame]:
    if frame_rgb is None:
        return None
    resized = _resize_for_model(frame_rgb)
    signature = _compute_signature_from_rgb(resized)
    if signature is None:
        return None
    try:
        bgr_frame = cv2.cvtColor(resized, cv2.COLOR_RGB2BGR)
    except Exception:
        bgr_frame = resized
    success, buffer = cv2.imencode(
        ".jpg",
        bgr_frame,
        [cv2.IMWRITE_JPEG_QUALITY, 82],
    )
    if not success:
        return None
    data = buffer.tobytes()
    return ExtractedFrame(
        name=f"{uuid.uuid4().hex[:8]}_{frame_idx}.jpg",
        data=data,
        mime_type="image/jpeg",
        signature=signature,
        total_frames=total_frames,
    )


def _iter_gif_frames(
    filename: str,
    wanted: Optional[int],
    *,
    stop_event: Event | None = None,
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
                preview_frames.append(_build_preview_frame(np.asarray(rgb_frame)))
                rgb_frame.close()
                if index + 1 >= total_frames:
                    break

            duration_seconds = duration_ms / 1000.0 if duration_ms else None
            target = _adaptive_cap(filename, wanted, total_frames, duration_seconds)
            motion = _motion_scores(preview_frames)
            indices = _select_motion_keyframes(total_frames, target, motion)

            for idx in indices:
                if stop_event and stop_event.is_set():
                    return
                try:
                    img.seek(idx)
                except EOFError:
                    break
                frame = img.convert("RGB")
                payload = _encode_rgb_frame(np.asarray(frame), idx, total_frames)
                frame.close()
                if payload is not None:
                    yield payload
    except Exception as exc:
        print(f"[iter_extracted_frames] Pillow GIF pipeline failed on {filename}: {exc}")


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
        yield from _iter_gif_frames(filename, wanted, stop_event=stop_event)
        return
    if ext in {".webp", ".apng", ".avif"}:
        try:
            with Image.open(filename) as img:
                total_frames = getattr(img, "n_frames", 1)
                target = _adaptive_cap(filename, wanted, total_frames, None)
                if target <= 0:
                    return
                idxs = np.linspace(0, max(total_frames - 1, 0), target, dtype=int)
                for idx in idxs:
                    if stop_event and stop_event.is_set():
                        return
                    img.seek(int(idx))
                    frame = img.convert("RGB")
                    payload = _encode_rgb_frame(np.asarray(frame), int(idx), int(total_frames) if total_frames else None)
                    frame.close()
                    if payload is not None:
                        yield payload
        except Exception as exc:
            print(f"[iter_extracted_frames] Pillow failed on {filename}: {exc}")
        return

    if _ffmpeg_tools_available():
        try:
            ffmpeg_yielded = False
            for payload in _iter_video_frames_ffmpeg(
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

    enable_parallel = bool(accelerated_tier) if accelerated_tier is not None else use_hwaccel
    enable_hwaccel = use_hwaccel and (accelerated_tier is None or accelerated_tier)
    hwaccel_in_use = enable_hwaccel

    def _open_capture(*, use_hw: bool) -> cv2.VideoCapture:
        with _suppress_cv2_stderr():
            cap_obj = cv2.VideoCapture(filename)
        if use_hw:
            try:
                with _suppress_cv2_stderr():
                    cap_obj.set(cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_ANY)
            except Exception:
                pass
            try:
                with _suppress_cv2_stderr():
                    cap_obj.set(cv2.CAP_PROP_BUFFERSIZE, 4)
            except Exception:
                pass
        return cap_obj

    cap = _open_capture(use_hw=hwaccel_in_use)

    try:
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
        duration_seconds = (total_frames / fps) if fps > 0 else None
        target = _adaptive_cap(filename, wanted, total_frames, duration_seconds)
        if target <= 0:
            return

        if total_frames > 0:
            idxs = np.linspace(0, max(total_frames - 1, 0), target, dtype=int)
        else:
            idxs = np.arange(0, target, dtype=int)
        idxs_list = list(dict.fromkeys(int(idx) for idx in idxs))
        if not idxs_list:
            return

        frame_seek_threshold = 5
        idx_iter = iter(idxs_list)
        pending_idx: int | None = None
        current_frame = int(cap.get(cv2.CAP_PROP_POS_FRAMES) or 0)

        executor: ThreadPoolExecutor | None = None
        pending: list[tuple[int, Future[Optional[ExtractedFrame]]]] = []
        max_pending = 1
        if enable_parallel:
            cpu_count = os.cpu_count() or 1
            workers = min(8, max(2, max(1, cpu_count // 2)))
            if workers > 1:
                executor = ThreadPoolExecutor(
                    max_workers=workers, thread_name_prefix="nsfw-frame"
                )
                max_pending = workers * 2

        decode_failures = 0
        max_decode_failures = max(8, len(idxs_list) // 3 + 3)
        hwaccel_fallback_attempted = False
        stop_requested = False

        try:
            while True:
                if stop_event and stop_event.is_set():
                    stop_requested = True
                    break

                if pending_idx is not None:
                    target_idx = pending_idx
                    pending_idx = None
                else:
                    try:
                        target_idx = next(idx_iter)
                    except StopIteration:
                        break

                if stop_event and stop_event.is_set():
                    stop_requested = True
                    break

                if target_idx > current_frame:
                    jump = target_idx - current_frame
                    seek_success = False
                    if jump > frame_seek_threshold:
                        try:
                            with _suppress_cv2_stderr():
                                seek_success = cap.set(cv2.CAP_PROP_POS_FRAMES, target_idx)
                        except Exception:
                            seek_success = False
                        else:
                            if not seek_success:
                                pos_after_seek = cap.get(cv2.CAP_PROP_POS_FRAMES)
                                if pos_after_seek is not None and int(pos_after_seek) == target_idx:
                                    seek_success = True
                        if seek_success:
                            current_frame = target_idx

                    if not seek_success:
                        skipped = 0
                        while skipped < jump:
                            if stop_event and stop_event.is_set():
                                stop_requested = True
                                break
                            with _suppress_cv2_stderr():
                                grabbed = cap.grab()
                            if not grabbed:
                                break
                            skipped += 1
                        current_frame += skipped
                        if stop_requested:
                            break
                        if skipped < jump:
                            break

                elif target_idx < current_frame:
                    seek_success = False
                    try:
                        with _suppress_cv2_stderr():
                            seek_success = cap.set(cv2.CAP_PROP_POS_FRAMES, target_idx)
                    except Exception:
                        seek_success = False
                    if seek_success:
                        current_frame = target_idx
                    else:
                        continue

                if stop_requested:
                    break

                with _suppress_cv2_stderr():
                    ok, frame = cap.read()
                if not ok:
                    decode_failures += 1
                    if hwaccel_in_use and not hwaccel_fallback_attempted:
                        print(
                            f"[iter_extracted_frames] Hardware decode failed at frame {target_idx}; retrying without acceleration."
                        )
                        hwaccel_fallback_attempted = True
                        hwaccel_in_use = False
                        try:
                            cap.release()
                        except Exception:
                            pass
                        cap = _open_capture(use_hw=False)
                        if target_idx > 0:
                            try:
                                with _suppress_cv2_stderr():
                                    cap.set(cv2.CAP_PROP_POS_FRAMES, target_idx)
                            except Exception:
                                pass
                        current_frame = int(cap.get(cv2.CAP_PROP_POS_FRAMES) or target_idx)
                        pending_idx = target_idx
                        decode_failures = 0
                        continue
                    fallback_seek = target_idx + 1
                    if total_frames > 0:
                        fallback_seek = min(total_frames - 1, fallback_seek)

                    if decode_failures >= max_decode_failures:
                        print(
                            f"[iter_extracted_frames] Decoder repeatedly failed around frame {target_idx}; aborting video scan for {os.path.basename(filename)}."
                        )
                        break
                    if decode_failures == 1:
                        print(
                            "[iter_extracted_frames] Failed to decode frame "
                            f"{target_idx} of {os.path.basename(filename)} "
                            f"(attempt {decode_failures}/{max_decode_failures}, "
                            f"hwaccel={'on' if hwaccel_in_use else 'off'}); "
                            f"seeking to frame {fallback_seek} before retrying."
                        )
                    try:
                        with _suppress_cv2_stderr():
                            cap.set(cv2.CAP_PROP_POS_FRAMES, fallback_seek)
                    except Exception:
                        pass
                    current_frame = int(cap.get(cv2.CAP_PROP_POS_FRAMES) or fallback_seek)
                    continue
                if stop_requested:
                    break

                decode_failures = 0

                if executor:
                    future = executor.submit(
                        _package_frame_for_yield, frame, target_idx, total_frames or None
                    )
                    pending.append((target_idx, future))
                    if len(pending) >= max_pending:
                        _, done_future = pending.pop(0)
                        if not stop_requested:
                            result = done_future.result()
                            if result is not None:
                                yield result
                else:
                    packaged = _package_frame_for_yield(
                        frame, target_idx, total_frames or None
                    )
                    if not stop_requested and packaged is not None:
                        yield packaged
                current_frame = int(cap.get(cv2.CAP_PROP_POS_FRAMES) or current_frame + 1)

            while pending:
                _, done_future = pending.pop(0)
                if stop_requested:
                    continue
                result = done_future.result()
                if result is not None:
                    yield result

        finally:
            if executor:
                executor.shutdown(wait=True, cancel_futures=False)
    except Exception as exc:
        print(f"[iter_extracted_frames] VideoCapture failed on {filename}: {exc}")
    finally:
        cap.release()


def _package_frame_for_yield(
    frame: np.ndarray, frame_idx: int, total_frames: Optional[int]
) -> Optional[ExtractedFrame]:
    if frame is None:
        return None
    try:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    except Exception:
        rgb = frame
    return _encode_rgb_frame(rgb, frame_idx, total_frames)


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
