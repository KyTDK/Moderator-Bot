from __future__ import annotations

import os
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event, Lock
from typing import Iterator, Optional

import cv2
import numpy as np

from .config import SUPPRESS_OPENCV_STDERR
from .encoding import package_bgr_frame
from .models import ExtractedFrame
from .sampling import adaptive_cap

_STDERR_REDIRECT_LOCK = Lock()


def detect_hwaccel_support() -> tuple[bool, str]:
    """
    Best-effort probe for OpenCV hardware decode availability.
    Returns (available, detail_message).
    """

    has_flags = all(
        hasattr(cv2, attr)
        for attr in ("CAP_PROP_HW_ACCELERATION", "VIDEO_ACCELERATION_ANY")
    )
    if not has_flags:
        return False, "OpenCV build missing hardware acceleration flags."

    cuda_module = getattr(cv2, "cuda", None)
    cuda_count = 0
    cuda_error: str | None = None
    if cuda_module is not None and hasattr(cuda_module, "getCudaEnabledDeviceCount"):
        try:
            cuda_count = int(cuda_module.getCudaEnabledDeviceCount())
        except Exception as exc:  # noqa: BLE001 - informative metadata
            cuda_error = f"CUDA detection failed ({exc})"
    else:
        cuda_error = "CUDA bindings unavailable in OpenCV."

    if cuda_count > 0:
        return True, f"CUDA devices detected ({cuda_count})."

    detail = cuda_error or "No CUDA-enabled devices detected."
    return False, detail


@contextmanager
def _suppress_cv2_stderr():
    if not SUPPRESS_OPENCV_STDERR:
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


def iter_video_frames_opencv(
    filename: str,
    wanted: Optional[int],
    *,
    use_hwaccel: bool = False,
    accelerated_tier: bool | None = None,
    stop_event: Event | None = None,
) -> Iterator[ExtractedFrame]:
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

    enable_parallel = bool(accelerated_tier) if accelerated_tier is not None else use_hwaccel
    enable_hwaccel = use_hwaccel and (accelerated_tier is None or accelerated_tier)
    hwaccel_in_use = enable_hwaccel

    cap = _open_capture(use_hw=hwaccel_in_use)

    try:
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
        duration_seconds = (total_frames / fps) if fps > 0 else None
        target = adaptive_cap(filename, wanted, total_frames, duration_seconds)
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
                    max_workers=workers,
                    thread_name_prefix="nsfw-frame",
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
                            f"[iter_extracted_frames] Decoder repeatedly failed around frame {target_idx}; "
                            f"aborting video scan for {os.path.basename(filename)}."
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
                        package_bgr_frame,
                        frame,
                        target_idx,
                        total_frames or None,
                    )
                    pending.append((target_idx, future))
                    if len(pending) >= max_pending:
                        _, done_future = pending.pop(0)
                        if not stop_requested:
                            result = done_future.result()
                            if result is not None:
                                yield result
                else:
                    packaged = package_bgr_frame(
                        frame,
                        target_idx,
                        total_frames or None,
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
