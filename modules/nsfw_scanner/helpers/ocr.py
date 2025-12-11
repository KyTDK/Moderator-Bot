from __future__ import annotations

import asyncio
import logging
from threading import Lock
from typing import Iterable, Sequence

try:
    from paddleocr import PaddleOCR  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    # ``paddleocr`` pulls in native dependencies (e.g. libGL via opencv) that may not
    # be present in every runtime. Treat any import failure as an unavailable optional
    # dependency instead of crashing at import time.
    PaddleOCR = None  # type: ignore

log = logging.getLogger(__name__)

_DEFAULT_LANGUAGE = "en"
_reader_lock = asyncio.Lock()
_reader: "PaddleOCR | None" = None
_reader_failed = False
_missing_dependency_logged = False
_reader_inference_lock = Lock()  # PaddleOCR is not thread-safe; serialize inference calls.


async def _load_reader(language: str | None = None) -> "PaddleOCR | None":
    global _reader, _reader_failed, _missing_dependency_logged
    if PaddleOCR is None:
        if not _missing_dependency_logged:
            log.warning("paddleocr is not installed; OCR text extraction is disabled.")
            _missing_dependency_logged = True
        return None
    if _reader_failed:
        return None
    if _reader is not None:
        return _reader

    async with _reader_lock:
        if _reader is not None:
            return _reader
        lang = (language or _DEFAULT_LANGUAGE).lower()

        def _create_reader() -> "PaddleOCR":
            return PaddleOCR(lang=lang, use_angle_cls=True, show_log=False)

        try:
            reader = await asyncio.to_thread(_create_reader)
        except Exception as exc:  # pragma: no cover - best-effort logging
            _reader_failed = True
            log.warning("Failed to initialize PaddleOCR reader: %s", exc, exc_info=True)
            return None
        _reader = reader
        return _reader


def _extract_text_segments(result: Iterable) -> list[str]:
    segments: list[str] = []
    for page in result or []:
        if not page:
            continue
        for entry in page:
            if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                continue
            text_info = entry[1]
            text_value = None
            if isinstance(text_info, (list, tuple)) and text_info:
                text_value = text_info[0]
            elif isinstance(text_info, str):
                text_value = text_info
            if text_value is None:
                continue
            text = str(text_value).strip()
            if text:
                segments.append(text)
    return segments


async def extract_text_from_image(
    image_path: str,
    *,
    min_chars: int = 12,
    max_chars: int = 4000,
    languages: Sequence[str] | None = None,
) -> str | None:
    """Run OCR over an image path and return consolidated text."""

    language = (languages[0] if languages else None) or _DEFAULT_LANGUAGE
    reader = await _load_reader(language)
    if reader is None:
        return None

    def _read_text():
        with _reader_inference_lock:
            return reader.ocr(image_path, cls=True)

    try:
        ocr_result = await asyncio.to_thread(_read_text)
    except Exception as exc:  # pragma: no cover - best-effort logging
        log.debug("OCR extraction failed for %s: %s", image_path, exc, exc_info=True)
        return None

    normalized_lines = _extract_text_segments(ocr_result)
    if not normalized_lines:
        return None

    combined = "\n".join(normalized_lines).strip()
    if len(combined) < min_chars:
        return None

    if len(combined) > max_chars:
        combined = combined[: max_chars - 3].rstrip() + "..."

    return combined


__all__ = ["extract_text_from_image"]
