import os
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator
from urllib.parse import urlparse

from ..constants import TMP_DIR

DEFAULT_CHUNK_SIZE = 1 << 17  # 128 KiB
MIN_CHUNK_SIZE = 1 << 15  # 32 KiB
MAX_CHUNK_SIZE = 1 << 20  # 1 MiB
DEFAULT_BUFFER_SIZE = 1 << 20  # 1 MiB
MAX_BUFFER_SIZE = 1 << 22  # 4 MiB
TARGET_CHUNK_SPLIT = 12


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
async def temp_download(session, url: str, ext: str | None = None) -> AsyncIterator[str]:
    if session is None:
        raise RuntimeError("NSFWScanner session is not initialised. Call start() first.")

    os.makedirs(TMP_DIR, exist_ok=True)

    if ext and not ext.startswith('.'):
        ext = '.' + ext
    ext = ext or os.path.splitext(urlparse(url).path)[1] or '.bin'

    path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}{ext}")

    try:
        async with session.get(url) as resp:
            resp.raise_for_status()
            chunk_size, buffer_limit = _resolve_stream_config(resp.content_length)
            with open(path, "wb") as file_obj:
                buffer = bytearray()
                async for chunk in resp.content.iter_chunked(chunk_size):
                    if not chunk:
                        continue
                    buffer.extend(chunk)
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
