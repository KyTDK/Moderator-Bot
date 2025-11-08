import logging
import os
import re
from typing import Any, Mapping

from modules.nsfw_scanner.constants import LOG_CHANNEL_ID
from modules.utils.log_channel import log_developer_issue

log = logging.getLogger(__name__)

_TRUNCATED_BYTES_PATTERN = re.compile(r"(\d+)\s+bytes?\s+not\s+processed", re.IGNORECASE)


def _get_file_size(path: str) -> int | None:
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def _format_metadata_value(key: str, value: Any) -> str | None:
    if value is None:
        return None

    if isinstance(value, bool):
        return "Yes" if value else "No"

    if key in {"source_bytes", "payload_bytes", "fallback_unprocessed_bytes"}:
        try:
            return f"{int(value):,}"
        except (TypeError, ValueError):
            return str(value)

    if key == "load_duration_ms":
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return str(value)

    if key == "image_size":
        if isinstance(value, (list, tuple)) and len(value) == 2:
            width, height = value
            try:
                width_int = int(width)
                height_int = int(height)
            except (TypeError, ValueError):
                pass
            else:
                return f"{width_int}x{height_int}"
        return str(value)

    if key == "source_url":
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            sanitized = stripped.replace("`", "'").replace("\n", " ").replace("\r", " ")
            return f"<{sanitized}>"
        return str(value)

    text = str(value).strip()
    if not text:
        return None
    sanitized = text.replace("`", "'").replace("\n", " ").replace("\r", " ")
    single_spaced = " ".join(sanitized.split())

    code_keys = {
        "path",
        "extension",
        "original_format",
        "image_mode",
        "fallback_mode",
        "fallback_result",
        "fallback_error",
        "guild_id",
    }
    if key in code_keys:
        return f"`{single_spaced}`"
    return single_spaced


def _extract_truncated_error_details(exc: Exception) -> dict[str, Any]:
    """Derive structured metadata for truncated image recovery logs."""
    message = str(exc)
    lower = message.lower()
    metadata: dict[str, Any] = {}

    match = _TRUNCATED_BYTES_PATTERN.search(message)
    if match:
        try:
            metadata["fallback_unprocessed_bytes"] = int(match.group(1))
        except (TypeError, ValueError):
            pass

    if "unrecognized data stream contents when reading image file" in lower:
        metadata["fallback_cause"] = (
            "Decoder saw unexpected data while reading the image stream."
        )
    elif "truncated file read" in lower:
        metadata["fallback_cause"] = "Decoder stopped early because the file stream ended."
    elif "image file is truncated" in lower:
        metadata["fallback_cause"] = (
            "Pillow reported truncated data; the file ended before decoding completed."
        )
    elif message:
        metadata["fallback_cause"] = message

    return metadata


def _format_image_log_details(
    *,
    display_name: str,
    filename: str,
    metadata: Mapping[str, Any] | None = None,
    error_summary: str | None = None,
) -> str:
    lines: list[str] = []
    sanitized_name = display_name.replace("`", "'").replace("\n", " ").replace("\r", " ")
    lines.append(f"**File**: `{sanitized_name}`")

    normalized_filename = filename or ""
    normalized_filename = normalized_filename.replace("\n", " ").replace("\r", " ")
    if normalized_filename and normalized_filename != display_name:
        safe_filename = normalized_filename.replace("`", "'")
        lines.append(f"**Path**: `{safe_filename}`")

    metadata = metadata or {}
    field_order = (
        ("guild_id", "Guild ID"),
        ("source_url", "Source URL"),
        ("source_host", "Source Host"),
        ("source_bytes", "Source Bytes"),
        ("extension", "Extension"),
        ("original_format", "Original Format"),
        ("image_mode", "Image Mode"),
        ("image_size", "Image Size"),
        ("conversion_requested", "Conversion Requested"),
        ("conversion_required", "Conversion Required"),
        ("passthrough", "Passthrough"),
        ("load_attempts", "Image Load Attempts"),
        ("fallback_attempted", "Fallback Attempted"),
        ("fallback_mode", "Fallback Mode"),
        ("fallback_result", "Fallback Result"),
        ("fallback_cause", "Fallback Cause"),
        ("fallback_unprocessed_bytes", "Fallback Unprocessed Bytes"),
        ("fallback_error", "Fallback Error"),
        ("load_duration_ms", "Load Duration (ms)"),
    )

    for key, label in field_order:
        formatted = _format_metadata_value(key, metadata.get(key))
        if formatted is None:
            continue
        lines.append(f"**{label}**: {formatted}")

    if error_summary:
        sanitized_error = error_summary.replace("`", "'").replace("\n", " ").replace("\r", " ")
        lines.append(f"**Error**: `{sanitized_error}`")

    return "\n".join(lines)


async def _notify_image_open_failure(
    scanner,
    *,
    filename: str,
    exc: Exception,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    if not LOG_CHANNEL_ID:
        return

    bot = getattr(scanner, "bot", None)
    if bot is None:
        return

    try:
        display_name = os.path.basename(filename) or filename
    except Exception:
        display_name = filename

    error_summary = f"{type(exc).__name__}: {exc}"
    details = _format_image_log_details(
        display_name=display_name,
        filename=filename,
        metadata=metadata,
        error_summary=error_summary,
    )
    await log_developer_issue(
        bot,
        summary="Failed to open image during NSFW scan.",
        details=details,
        severity="error",
        context="nsfw_scanner.image_open",
        logger=log,
    )


async def _notify_truncated_image_recovery(
    scanner,
    *,
    filename: str,
    exc: Exception,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    if not LOG_CHANNEL_ID:
        return

    bot = getattr(scanner, "bot", None)
    if bot is None:
        return

    try:
        display_name = os.path.basename(filename) or filename
    except Exception:
        display_name = filename

    details = _format_image_log_details(
        display_name=display_name,
        filename=filename,
        metadata=metadata,
        error_summary=f"{type(exc).__name__}: {exc}",
    )
    await log_developer_issue(
        bot,
        summary="Recovered truncated image during NSFW scan.",
        details=details,
        severity="warning",
        context="nsfw_scanner.image_open",
        logger=log,
    )


__all__ = [
    "log",
    "log_developer_issue",
    "_get_file_size",
    "_format_metadata_value",
    "_format_image_log_details",
    "_extract_truncated_error_details",
    "_notify_image_open_failure",
    "_notify_truncated_image_recovery",
]
