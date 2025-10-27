from __future__ import annotations

import os
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import Event
from typing import Iterator, Optional

import cv2
import numpy as np
from PIL import Image, ImageSequence

_FFMPEG_LOG_ENV = "OPENCV_FFMPEG_CAPTURE_OPTIONS"
_FFMPEG_LOG_QUIET = "loglevel;quiet"


def _configure_video_logging() -> None:
    """Tame OpenCV/FFmpeg logging so noisy decoder errors stay out of stderr."""
    existing = os.environ.get(_FFMPEG_LOG_ENV, "")
    options = [opt for opt in existing.split("|") if opt]
    if all(not opt.lower().startswith("loglevel;") for opt in options):
        options.append(_FFMPEG_LOG_QUIET)
        os.environ[_FFMPEG_LOG_ENV] = "|".join(options)
    try:
        cv2.setLogLevel(cv2.LOG_LEVEL_ERROR)
    except AttributeError:
        try:
            cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)  # type: ignore[attr-defined]
        except Exception:
            pass
    except Exception:
        pass


_configure_video_logging()

__all__ = [
    "ExtractedFrame",
    "iter_extracted_frames",
    "frames_are_similar",
]

PREVIEW_MAX_DIMENSION = 112
TARGET_MAX_DIMENSION = 224
DEDUP_SIGNATURE_DIM = 16


@dataclass(slots=True)
class ExtractedFrame:
    name: str
    data: bytes
    mime_type: str
    signature: Optional[np.ndarray]
    total_frames: Optional[int] = None


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

    enable_parallel = bool(accelerated_tier) if accelerated_tier is not None else use_hwaccel
    enable_hwaccel = use_hwaccel and (accelerated_tier is None or accelerated_tier)

    cap = cv2.VideoCapture(filename)
    if enable_hwaccel:
        try:
            cap.set(cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_ANY)
        except Exception:
            pass
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 4)
        except Exception:
            pass

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

        stop_requested = False
        consecutive_failures = 0

        try:
            for target_idx in idx_iter:
                if stop_event and stop_event.is_set():
                    stop_requested = True
                    break

                if target_idx > current_frame:
                    jump = target_idx - current_frame
                    seek_success = False
                    if jump > frame_seek_threshold:
                        try:
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
                        seek_success = cap.set(cv2.CAP_PROP_POS_FRAMES, target_idx)
                    except Exception:
                        seek_success = False
                    if seek_success:
                        current_frame = target_idx
                    else:
                        continue

                if stop_requested:
                    break

                ok, frame = cap.read()
                if not ok:
                    # ffmpeg occasionally reports decoding errors such as
                    # ``h264 @ ... mmco: unref short failure`` when it
                    # encounters damaged frames.  OpenCV surfaces this as a
                    # ``False`` read result which previously caused us to
                    # stop processing the rest of the video.  Instead, try to
                    # skip over the bad frame and continue so we can still
                    # analyse any subsequent frames that decode correctly.
                    consecutive_failures += 1
                    if consecutive_failures > 8:
                        break
                    if stop_event and stop_event.is_set():
                        break
                    try:
                        cap.grab()
                    except Exception:
                        pass
                    current_frame = int(cap.get(cv2.CAP_PROP_POS_FRAMES) or current_frame + 1)
                    continue
                consecutive_failures = 0
                if stop_requested:
                    break

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
