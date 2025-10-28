import os
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator
from urllib.parse import urlparse, urlunparse

import aiohttp

from ..constants import DEFAULT_DOWNLOAD_CAP_BYTES, TMP_DIR

DEFAULT_CHUNK_SIZE = 1 << 17  # 128 KiB
MIN_CHUNK_SIZE = 1 << 15  # 32 KiB
MAX_CHUNK_SIZE = 1 << 20  # 1 MiB
DEFAULT_BUFFER_SIZE = 1 << 20  # 1 MiB
MAX_BUFFER_SIZE = 1 << 22  # 4 MiB
TARGET_CHUNK_SPLIT = 12
PROBE_LIMIT_BYTES = 512 * 1024  # 512 KiB
TENOR_VIDEO_EXTS = (".mp4", ".webm")


def _is_tenor_host(host: str) -> bool:
    host = host.lower()
    return host == "tenor.com" or host.endswith(".tenor.com")


async def _probe_head(session, url: str) -> tuple[bool, int | None]:
    try:
        async with session.head(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status < 400:
                length_header = resp.headers.get("Content-Length")
                content_length = int(length_header) if length_header and length_header.isdigit() else None
                return True, content_length
    except Exception:
        return False, None
    return False, None


async def resolve_media_url(session, url: str, *, prefer_video: bool = True) -> str:
    if not prefer_video:
        return url
    parsed = urlparse(url)
    if not _is_tenor_host(parsed.netloc):
        return url
    base, ext = os.path.splitext(parsed.path)
    if ext.lower() != ".gif":
        return url

    for alt_ext in TENOR_VIDEO_EXTS:
        alt_path = f"{base}{alt_ext}"
        alt_url = urlunparse(parsed._replace(path=alt_path))
        ok, _ = await _probe_head(session, alt_url)
        if ok:
            return alt_url
    return url


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
) -> AsyncIterator[str]:
    if session is None:
        raise RuntimeError("NSFWScanner session is not initialised. Call start() first.")

    os.makedirs(TMP_DIR, exist_ok=True)

    resolved_url = await resolve_media_url(session, url, prefer_video=prefer_video)
    unlimited = download_cap_bytes is None
    if ext and not ext.startswith('.'):
        ext = '.' + ext
    resolved_path_ext = os.path.splitext(urlparse(resolved_url).path)[1]
    ext = ext or resolved_path_ext or '.bin'
    head_ok, head_length = await _probe_head(session, resolved_url)
    if not unlimited and head_ok and head_length and head_length > download_cap_bytes:
        if resolved_url != url:
            resolved_url = url
            head_ok, head_length = await _probe_head(session, resolved_url)
            if head_ok and head_length and head_length > download_cap_bytes:
                raise ValueError(f"Download exceeds cap ({head_length} bytes)")
        else:
            raise ValueError(f"Download exceeds cap ({head_length} bytes)")

    path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}{ext}")

    try:
        async with session.get(resolved_url) as resp:
            resp.raise_for_status()
            response_length = resp.content_length or head_length
            chunk_size, buffer_limit = _resolve_stream_config(response_length)
            total_downloaded = 0
            with open(path, "wb") as file_obj:
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
                    if not unlimited and total_downloaded > download_cap_bytes:
                        raise ValueError("Download exceeded cap")
                    if len(buffer) >= buffer_limit:
                        file_obj.write(buffer)
                        buffer = bytearray()
                if buffer:
                    file_obj.write(buffer)
    except Exception:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        raise

    try:
        yield path
    finally:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
