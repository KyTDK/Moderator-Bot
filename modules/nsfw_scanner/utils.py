import base64
import io
import os
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import Event
from typing import Iterator, Optional, Tuple

import cv2
import filetype
import numpy as np
from PIL import Image


@dataclass(slots=True)
class ExtractedFrame:
    name: str
    data: bytes
    mime_type: str
    signature: Optional[np.ndarray]
    total_frames: Optional[int] = None


def safe_delete(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def is_allowed_category(category: str, allowed_categories) -> bool:
    normalized = category.replace("/", "_").replace("-", "_").lower()
    allowed = [c.lower() for c in allowed_categories]
    return normalized in allowed


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
    ext = os.path.splitext(filename)[1].lower()
    kind = filetype.guess(filename)

    if not kind:
        if ext in ANIMATED_EXTS:
            return FILE_TYPE_VIDEO, None
        if ext in VIDEO_EXTS:
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
                with Image.open(filename) as im:
                    if getattr(im, "is_animated", False) or getattr(im, "n_frames", 1) > 1:
                        return FILE_TYPE_VIDEO, mime
            except Exception:
                return FILE_TYPE_VIDEO, mime
        return FILE_TYPE_IMAGE, mime

    return FILE_TYPE_UNKNOWN, mime or None


def _resolve_frame_target(wanted: Optional[int], total: int) -> int:
    if total <= 0:
        return wanted or 0
    if wanted is None:
        return total
    try:
        desired = int(wanted)
    except (TypeError, ValueError):
        return 0
    if desired <= 0:
        return 0
    return min(desired, total)


def iter_extracted_frames(
    filename: str,
    wanted: Optional[int],
    *,
    use_hwaccel: bool = False,
    accelerated_tier: bool | None = None,
    stop_event: Event | None = None,
) -> Iterator[ExtractedFrame]:
    ext = os.path.splitext(filename)[1].lower()
    if ext in {".webp", ".apng", ".avif"}:
        try:
            with Image.open(filename) as img:
                total_frames = getattr(img, "n_frames", 1)
                target = _resolve_frame_target(wanted, total_frames)
                if target <= 0:
                    return
                idxs = np.linspace(0, max(total_frames - 1, 0), target, dtype=int)
                for idx in idxs:
                    if stop_event and stop_event.is_set():
                        return
                    img.seek(int(idx))
                    frame = img.convert("RGBA")
                    buffer = io.BytesIO()
                    try:
                        frame.save(buffer, format="PNG")
                        data = buffer.getvalue()
                    finally:
                        buffer.close()
                    signature = _compute_signature_from_pillow(frame)
                    frame.close()
                    yield ExtractedFrame(
                        name=f"{uuid.uuid4().hex[:8]}_{idx}.png",
                        data=data,
                        mime_type="image/png",
                        signature=signature,
                        total_frames=int(total_frames) if total_frames else None,
                    )
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
        target = _resolve_frame_target(wanted, total_frames)
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
                                # Some OpenCV builds return False even when seeking succeeds
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
                    break
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


def _compute_signature_from_cv_frame(frame: np.ndarray) -> Optional[np.ndarray]:
    if frame is None:
        return None
    try:
        grayscale = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        resized = cv2.resize(grayscale, (32, 32), interpolation=cv2.INTER_AREA)
    except Exception:
        return None
    return resized.astype("float32") / 255.0


def _compute_signature_from_pillow(image: Image.Image) -> Optional[np.ndarray]:
    try:
        grayscale = image.convert("L")
        resized = grayscale.resize((32, 32), Image.BILINEAR)
        return np.asarray(resized, dtype="float32") / 255.0
    except Exception:
        return None


def _package_frame_for_yield(
    frame: np.ndarray, frame_idx: int, total_frames: Optional[int]
) -> Optional[ExtractedFrame]:
    success, buffer = cv2.imencode(
        ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80]
    )
    if not success:
        return None
    data = buffer.tobytes()
    signature = _compute_signature_from_cv_frame(frame)
    return ExtractedFrame(
        name=f"{uuid.uuid4().hex[:8]}_{frame_idx}.jpg",
        data=data,
        mime_type="image/jpeg",
        signature=signature,
        total_frames=total_frames,
    )


def frames_are_similar(
    signature_a: Optional[np.ndarray],
    signature_b: Optional[np.ndarray],
    *,
    threshold: float = 0.99,
) -> bool:
    if signature_a is None or signature_b is None:
        return False
    if signature_a.shape != signature_b.shape:
        return False
    diff = np.abs(signature_a - signature_b).mean()
    similarity = 1.0 - diff
    return similarity >= threshold


def file_to_b64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()
