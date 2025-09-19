import base64
import os
import uuid
from typing import Optional

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

def determine_file_type(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    kind = filetype.guess(filename)

    if not kind:
        if ext in ANIMATED_EXTS:
            return "Video"
        if ext in VIDEO_EXTS:
            return "Video"
        if ext in IMAGE_EXTS:
            return "Image"
        return "Unknown"

    mime = kind.mime

    if mime.startswith("video"):
        return "Video"

    if mime.startswith("image"):
        if ext in ANIMATED_EXTS:
            try:
                with Image.open(filename) as im:
                    if getattr(im, "is_animated", False) or getattr(im, "n_frames", 1) > 1:
                        return "Video"
            except Exception:
                return "Video"
        return "Image"

    return mime


def extract_frames_threaded(filename: str, wanted: Optional[int]) -> list[str]:
    temp_frames: list[str] = []

    def resolve_target(total: int) -> int:
        if total <= 0:
            return 0
        if wanted is None:
            return total
        try:
            desired = int(wanted)
        except (TypeError, ValueError):
            return 0
        if desired <= 0:
            return 0
        return min(desired, total)

    ext = os.path.splitext(filename)[1].lower()
    if ext in {".webp", ".apng", ".avif"}:
        try:
            with Image.open(filename) as img:
                n = getattr(img, "n_frames", 1)
                if n <= 1:
                    return []
                target = resolve_target(n)
                if target <= 0:
                    return []
                idxs = np.linspace(0, n - 1, target, dtype=int)
                for idx in idxs:
                    img.seek(int(idx))
                    frame = img.convert("RGBA")
                    out = os.path.join(TMP_DIR, f"{uuid.uuid4().hex[:8]}_{idx}.png")
                    frame.save(out, format="PNG")
                    temp_frames.append(out)
                return temp_frames
        except Exception as e:
            print(f"[extract_frames_threaded] Pillow failed on {filename}: {e}")

    cap = cv2.VideoCapture(filename)
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        target = resolve_target(total)
        if target <= 0:
            return []

        idxs = set(np.linspace(0, total - 1, target, dtype=int))
        if not idxs:
            return []

        max_idx = max(idxs)
        current_frame = 0
        while cap.isOpened() and current_frame <= max_idx:
            ok, frame = cap.read()
            if not ok:
                break

            if current_frame in idxs:
                out_name = os.path.join(
                    TMP_DIR, f"{uuid.uuid4().hex[:8]}_{current_frame}.jpg"
                )
                cv2.imwrite(out_name, frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                temp_frames.append(out_name)
                if len(temp_frames) == len(idxs):
                    break

            current_frame += 1

        return temp_frames
    except Exception as e:
        print(f"[extract_frames_threaded] VideoCapture failed on {filename}: {e}")
        return []
    finally:
        cap.release()

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
