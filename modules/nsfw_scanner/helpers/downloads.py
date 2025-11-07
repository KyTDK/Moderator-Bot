from __future__ import annotations

import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator
from urllib.parse import urlparse, urlunparse

import aiohttp
from yarl import URL

from ..constants import DEFAULT_DOWNLOAD_CAP_BYTES, TMP_DIR
from ..utils.file_ops import safe_delete

DEFAULT_CHUNK_SIZE = 1 << 17  # 128 KiB
MIN_CHUNK_SIZE = 1 << 15  # 32 KiB
MAX_CHUNK_SIZE = 1 << 20  # 1 MiB
DEFAULT_BUFFER_SIZE = 1 << 20  # 1 MiB
MAX_BUFFER_SIZE = 1 << 22  # 4 MiB
TARGET_CHUNK_SPLIT = 12
PROBE_LIMIT_BYTES = 512 * 1024  # 512 KiB
TENOR_VIDEO_EXTS = (".mp4", ".webm")
_DISCORD_EMOJI_HOSTS = {
    "cdn.discordapp.com",
    "cdn.discordapp.net",
    "media.discordapp.com",
    "media.discordapp.net",
}


def is_tenor_host(host: str) -> bool:
    host = host.lower()
    return host == "tenor.com" or host.endswith(".tenor.com")


def _prepare_request_url(url: str) -> URL | str:
    """Preserve percent-encoded segments when handing URLs to aiohttp."""
    if "%" not in url:
        return url
    try:
        return URL(url, encoded=True)
    except ValueError:
        return url


async def _probe_head(session, url: str) -> tuple[bool, int | None, float | None]:
    started = time.perf_counter()
    try:
        async with session.head(
            _prepare_request_url(url),
            allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            if resp.status < 400:
                length_header = resp.headers.get("Content-Length")
                content_length = (
                    int(length_header)
                    if length_header and length_header.isdigit()
                    else None
                )
                return True, content_length, (time.perf_counter() - started) * 1000
    except Exception:
        return False, None, (time.perf_counter() - started) * 1000
    return False, None, (time.perf_counter() - started) * 1000


async def resolve_media_url(session, url: str, *, prefer_video: bool = True) -> str:
    if not prefer_video:
        return url
    parsed = urlparse(url)
    if not is_tenor_host(parsed.netloc):
        return url
    base, ext = os.path.splitext(parsed.path)
    if ext.lower() != ".gif":
        return url

    for alt_ext in TENOR_VIDEO_EXTS:
        alt_path = f"{base}{alt_ext}"
        alt_url = urlunparse(parsed._replace(path=alt_path))
        ok, _, _ = await _probe_head(session, alt_url)
        if ok:
            return alt_url
    return url


def _expand_discord_emoji_variants(url: str) -> list[str]:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in _DISCORD_EMOJI_HOSTS:
        return [url]
    if not parsed.path.startswith("/emojis/"):
        return [url]
    base, ext = os.path.splitext(parsed.path)
    if ext.lower() != ".gif":
        return [url]
    variants = [url]
    for alt_ext in (".webp", ".png"):
        alt_path = f"{base}{alt_ext}"
        variants.append(urlunparse(parsed._replace(path=alt_path)))
    return variants


def _build_download_candidates(original_url: str, resolved_url: str) -> list[str]:
    seen: set[str] = set()
    candidates: list[str] = []

    def _add(url: str) -> None:
        for variant in _expand_discord_emoji_variants(url):
            if variant in seen:
                continue
            seen.add(variant)
            candidates.append(variant)

    _add(resolved_url)
    if resolved_url != original_url:
        _add(original_url)
    return candidates


def _resolve_effective_ext(provided_ext: str | None, candidate_url: str) -> str:
    if provided_ext:
        return provided_ext if provided_ext.startswith(".") else f".{provided_ext}"
    candidate_ext = os.path.splitext(urlparse(candidate_url).path)[1]
    return candidate_ext or ".bin"


def _cap_error(
    *,
    unlimited: bool,
    head_ok: bool,
    head_length: int | None,
    download_cap_bytes: int | None,
) -> ValueError | None:
    if (
        unlimited
        or not head_ok
        or not head_length
        or download_cap_bytes is None
    ):
        return None
    if head_length > download_cap_bytes:
        return ValueError(f"Download exceeds cap ({head_length} bytes)")
    return None


class TempDownloadTelemetry:
    """Detailed timings captured during a temporary download."""

    __slots__ = (
        "resolve_latency_ms",
        "head_latency_ms",
        "download_latency_ms",
        "disk_write_latency_ms",
        "bytes_downloaded",
        "content_length",
        "resolved_url",
    )

    def __init__(self) -> None:
        self.resolve_latency_ms: float | None = None
        self.head_latency_ms: float = 0.0
        self.download_latency_ms: float | None = None
        self.disk_write_latency_ms: float = 0.0
        self.bytes_downloaded: int | None = None
        self.content_length: int | None = None
        self.resolved_url: str | None = None

    def record_head_duration(self, duration_ms: float | None) -> None:
        if duration_ms is None:
            return
        try:
            duration_value = float(duration_ms)
        except (TypeError, ValueError):
            return
        if duration_value <= 0:
            return
        self.head_latency_ms += duration_value

    def record_disk_write(self, duration_ms: float | None) -> None:
        if duration_ms is None:
            return
        try:
            duration_value = float(duration_ms)
        except (TypeError, ValueError):
            return
        if duration_value <= 0:
            return
        self.disk_write_latency_ms += duration_value


class TempDownloadResult:
    __slots__ = ("path", "telemetry")

    def __init__(self, path: str, telemetry: TempDownloadTelemetry) -> None:
        self.path = path
        self.telemetry = telemetry

    def __str__(self) -> str:  # pragma: no cover - trivial delegation
        return self.path

    def __fspath__(self) -> str:  # pragma: no cover - trivial delegation
        return self.path

    def __bytes__(self) -> bytes:  # pragma: no cover - trivial delegation
        return self.path.encode()

    def __getattr__(self, attr):  # pragma: no cover - trivial delegation
        return getattr(self.path, attr)


def _resolve_stream_config(content_length: int | None) -> tuple[int, int]:
    """Determine chunk and buffer sizes for the incoming payload."""
    if not content_length or content_length <= 0:
        return DEFAULT_CHUNK_SIZE, DEFAULT_BUFFER_SIZE

    approx_chunk = max(content_length // TARGET_CHUNK_SPLIT, MIN_CHUNK_SIZE)
    chunk_size = max(MIN_CHUNK_SIZE, min(MAX_CHUNK_SIZE, approx_chunk))

    buffer_limit = max(
        chunk_size,
        min(MAX_BUFFER_SIZE, max(DEFAULT_BUFFER_SIZE, chunk_size * 2)),
    )
    return chunk_size, buffer_limit

@asynccontextmanager
async def temp_download(
    session,
    url: str,
    ext: str | None = None,
    *,
    prefer_video: bool = True,
    download_cap_bytes: int | None = DEFAULT_DOWNLOAD_CAP_BYTES,
) -> AsyncIterator[TempDownloadResult]:
    if session is None:
        raise RuntimeError("NSFWScanner session is not initialised. Call start() first.")

    os.makedirs(TMP_DIR, exist_ok=True)

    telemetry = TempDownloadTelemetry()

    resolve_started = time.perf_counter()
    resolved_url = await resolve_media_url(session, url, prefer_video=prefer_video)
    telemetry.resolve_latency_ms = (time.perf_counter() - resolve_started) * 1000
    unlimited = download_cap_bytes is None

    download_path: str | None = None
    last_exc: Exception | None = None

    candidates = _build_download_candidates(url, resolved_url)

    for candidate_url in candidates:
        effective_ext = _resolve_effective_ext(ext, candidate_url)

        head_ok, head_length, head_duration = await _probe_head(session, candidate_url)
        telemetry.record_head_duration(head_duration)
        telemetry.content_length = head_length
        telemetry.resolved_url = candidate_url

        cap_exc = _cap_error(
            unlimited=unlimited,
            head_ok=head_ok,
            head_length=head_length,
            download_cap_bytes=download_cap_bytes,
        )
        if cap_exc is not None:
            last_exc = cap_exc
            continue

        candidate_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}{effective_ext}")

        try:
            download_started = time.perf_counter()
            async with session.get(_prepare_request_url(candidate_url)) as resp:
                resp.raise_for_status()
                response_length = resp.content_length or head_length
                chunk_size, buffer_limit = _resolve_stream_config(response_length)
                total_downloaded = 0
                with open(candidate_path, "wb") as file_obj:
                    buffer = bytearray()
                    async for chunk in resp.content.iter_chunked(chunk_size):
                        if not chunk:
                            continue
                        buffer.extend(chunk)
                        total_downloaded += len(chunk)
                        if (
                            not unlimited
                            and response_length is None
                            and total_downloaded > PROBE_LIMIT_BYTES
                        ):
                            raise ValueError("Download exceeded probe window")
                        if (
                            not unlimited
                            and download_cap_bytes is not None
                            and total_downloaded > download_cap_bytes
                        ):
                            raise ValueError("Download exceeded cap")
                        if len(buffer) >= buffer_limit:
                            write_started = time.perf_counter()
                            file_obj.write(buffer)
                            telemetry.record_disk_write(
                                (time.perf_counter() - write_started) * 1000
                            )
                            buffer.clear()
                    if buffer:
                        write_started = time.perf_counter()
                        file_obj.write(buffer)
                        telemetry.record_disk_write(
                            (time.perf_counter() - write_started) * 1000
                        )
                telemetry.download_latency_ms = (
                    (time.perf_counter() - download_started) * 1000
                )
                telemetry.bytes_downloaded = total_downloaded
            download_path = candidate_path
            break
        except aiohttp.ClientResponseError as exc:
            last_exc = exc
            safe_delete(candidate_path)
            if exc.status in {404, 415}:
                continue
            raise
        except Exception as exc:
            last_exc = exc
            safe_delete(candidate_path)
            raise

    if download_path is None:
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Failed to download media: no candidates succeeded")

    try:
        yield TempDownloadResult(path=download_path, telemetry=telemetry)
    finally:
        safe_delete(download_path)
