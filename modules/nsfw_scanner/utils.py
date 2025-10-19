import base64
import os
import uuid
from threading import Event
from typing import Iterator, Optional, Tuple

import cv2
import filetype
import numpy as np
from PIL import Image

from .constants import TMP_DIR

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
    stop_event: Event | None = None,
) -> Iterator[str]:
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
                    out = os.path.join(TMP_DIR, f"{uuid.uuid4().hex[:8]}_{idx}.png")
                    frame.save(out, format="PNG")
                    yield out
        except Exception as exc:
            print(f"[iter_extracted_frames] Pillow failed on {filename}: {exc}")
        return

    cap = cv2.VideoCapture(filename)
    if use_hwaccel:
        try:
            cap.set(cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_ANY)
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

        last_idx = idxs_list[-1]
        idx_iter = iter(idxs_list)
        next_idx = next(idx_iter)
        current_frame = 0
        while cap.isOpened() and current_frame <= last_idx:
            if stop_event and stop_event.is_set():
                return
            ok, frame = cap.read()
            if not ok:
                break

            if current_frame >= next_idx:
                out_name = os.path.join(
                    TMP_DIR, f"{uuid.uuid4().hex[:8]}_{current_frame}.jpg"
                )
                cv2.imwrite(out_name, frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                yield out_name
                try:
                    next_idx = next(idx_iter)
                except StopIteration:
                    break
            current_frame += 1
    except Exception as exc:
        print(f"[iter_extracted_frames] VideoCapture failed on {filename}: {exc}")
    finally:
        cap.release()


def extract_frames_threaded(filename: str, wanted: Optional[int]) -> list[str]:
    return list(iter_extracted_frames(filename, wanted))


def compute_frame_signature(path: str) -> Optional[np.ndarray]:
    frame = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if frame is None:
        return None
    try:
        resized = cv2.resize(frame, (32, 32), interpolation=cv2.INTER_AREA)
    except Exception:
        return None
    return resized.astype("float32") / 255.0


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

def convert_to_png_safe(input_path: str, output_path: str) -> Optional[str]:
    try:
        with Image.open(input_path) as img:
            img = img.convert("RGBA")
            img.save(output_path, format="PNG")
        return output_path
    except Exception as e:
        print(f"[convert] Failed to convert {input_path} to PNG: {e}")
        return None
