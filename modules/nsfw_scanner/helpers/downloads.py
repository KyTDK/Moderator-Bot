from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator
from urllib.parse import urlparse, urlunparse

import aiohttp

from ..concurrency import concurrency_pool
from ..limits import PremiumLimits
from ..utils.file_ops import safe_delete
from ..constants import DEFAULT_DOWNLOAD_CAP_BYTES, TMP_DIR

DEFAULT_CHUNK_SIZE = 1 << 17  # 128 KiB
MIN_CHUNK_SIZE = 1 << 15  # 32 KiB
MAX_CHUNK_SIZE = 1 << 20  # 1 MiB
DEFAULT_BUFFER_SIZE = 1 << 20  # 1 MiB
MAX_BUFFER_SIZE = 1 << 22  # 4 MiB
TARGET_CHUNK_SPLIT = 12
PROBE_LIMIT_BYTES = 512 * 1024  # 512 KiB
TENOR_VIDEO_EXTS = (".mp4", ".webm")


@dataclass(slots=True)
class DownloadResult:
    path: str
    url: str
    content_type: str | None
    bytes_downloaded: int | None


def _is_tenor_host(host: str) -> bool:
    host = host.lower()
    return host == "tenor.com" or host.endswith(".tenor.com")


async def _probe_head(
    session: aiohttp.ClientSession,
    url: str,
    *,
    timeout: float = 5.0,
) -> tuple[bool, int | None]:
    try:
        async with session.head(
            url,
            allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status < 400:
                length_header = resp.headers.get("Content-Length")
                if length_header and length_header.isdigit():
                    return True, int(length_header)
                return True, None
    except Exception:
        return False, None
    return False, None


async def resolve_media_url(
    session: aiohttp.ClientSession,
    url: str,
    *,
    prefer_video: bool = True,
    head_cache: dict[str, tuple[bool, int | None]] | None = None,
) -> str:
    if not prefer_video:
        return url
    parsed = urlparse(url)
    if not _is_tenor_host(parsed.netloc):
        return url
    base, ext = os.path.splitext(parsed.path)
    if ext.lower() != ".gif":
        return url

    cache = head_cache or {}
    for alt_ext in TENOR_VIDEO_EXTS:
        alt_path = f"{base}{alt_ext}"
        alt_url = urlunparse(parsed._replace(path=alt_path))
        cache_key = f"HEAD::{alt_url}"
        cached = cache.get(cache_key)
        if cached is None:
            cached = await _probe_head(session, alt_url)
            cache[cache_key] = cached
        ok, _ = cached
        if ok:
            return alt_url
    return url


def _resolve_stream_config(content_length: int | None) -> tuple[int, int]:
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
    session: aiohttp.ClientSession,
    url: str,
    *,
    guild_key: str | int | None,
    limits: PremiumLimits,
    ext: str | None = None,
    prefer_video: bool = True,
    head_cache: dict[str, tuple[bool, int | None]] | None = None,
) -> AsyncIterator[DownloadResult]:
    if session is None:
        raise RuntimeError("NSFWScanner session is not initialised. Call start() first.")

    os.makedirs(TMP_DIR, exist_ok=True)

    head_cache = head_cache or {}
    resolved_url = await resolve_media_url(
        session,
        url,
        prefer_video=prefer_video,
        head_cache=head_cache,
    )

    if ext and not ext.startswith("."):
        ext = f".{ext}"
    if not ext:
        resolved_path_ext = os.path.splitext(urlparse(resolved_url).path)[1]
        ext = resolved_path_ext or ".bin"

    cache_key = f"HEAD::{resolved_url}"
    head_ok, head_length = head_cache.get(cache_key, (False, None))
    if cache_key not in head_cache:
        head_ok, head_length = await _probe_head(session, resolved_url)
        head_cache[cache_key] = (head_ok, head_length)

    download_cap_bytes = limits.download_cap_bytes
    unlimited = download_cap_bytes is None
    effective_cap = download_cap_bytes if download_cap_bytes is not None else DEFAULT_DOWNLOAD_CAP_BYTES
    if (
        not unlimited
        and head_ok
        and head_length is not None
        and head_length > effective_cap
    ):
        raise ValueError(f"Download exceeds cap ({head_length} bytes)")

    tmp_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}{ext}")

    async with concurrency_pool.limit(guild_key or "global", "download", limits.max_downloads):
        try:
            async with session.get(resolved_url) as resp:
                resp.raise_for_status()
                content_length = resp.content_length or head_length
                chunk_size, buffer_limit = _resolve_stream_config(content_length)
                total_downloaded = 0
                content_type = resp.headers.get("Content-Type")
                buffer = bytearray()

                async def _flush() -> None:
                    nonlocal buffer
                    if not buffer:
                        return
                    data = bytes(buffer)
                    buffer = bytearray()
                    await asyncio.to_thread(_write_bytes, tmp_path, data, True)

                async for chunk in resp.content.iter_chunked(chunk_size):
                    if not chunk:
                        continue
                    buffer.extend(chunk)
                    total_downloaded += len(chunk)

                    if (
                        not unlimited
                        and content_length is None
                        and total_downloaded > PROBE_LIMIT_BYTES
                    ):
                        raise ValueError("Download exceeded probe window")

                    if not unlimited and effective_cap is not None and total_downloaded > effective_cap:
                        raise ValueError("Download exceeded cap")

                    if len(buffer) >= buffer_limit:
                        await _flush()

                if buffer:
                    await _flush()

        except Exception:
            safe_delete(tmp_path)
            raise

        try:
            yield DownloadResult(
                path=tmp_path,
                url=resolved_url,
                content_type=content_type,
                bytes_downloaded=total_downloaded,
            )
        finally:
            safe_delete(tmp_path)


def _write_bytes(path: str, data: bytes, append: bool = True) -> None:
    mode = "ab" if append else "wb"
    with open(path, mode) as file_obj:
        file_obj.write(data)


__all__ = ["temp_download", "resolve_media_url", "DownloadResult"]
