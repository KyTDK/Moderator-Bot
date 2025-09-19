import os
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator
from urllib.parse import urlparse

import aiofiles

from ..constants import TMP_DIR

@asynccontextmanager
async def temp_download(session, url: str, ext: str | None = None) -> AsyncIterator[str]:
    if session is None:
        raise RuntimeError("NSFWScanner session is not initialised. Call start() first.")

    os.makedirs(TMP_DIR, exist_ok=True)

    if ext and not ext.startswith('.'):
        ext = '.' + ext
    ext = ext or os.path.splitext(urlparse(url).path)[1] or '.bin'

    path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}{ext}")

    async with session.get(url) as resp:
        resp.raise_for_status()
        async with aiofiles.open(path, "wb") as file_obj:
            async for chunk in resp.content.iter_chunked(1 << 14):
                await file_obj.write(chunk)

    try:
        yield path
    finally:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
