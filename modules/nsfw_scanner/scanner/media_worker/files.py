from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import AsyncExitStack
from urllib.parse import urlparse

import discord
from apnggif import apnggif

from modules.nsfw_scanner.constants import TMP_DIR
from modules.nsfw_scanner.scanner.work_item import MediaWorkItem
from modules.nsfw_scanner.utils.file_ops import safe_delete

async def convert_apng(stack: AsyncExitStack, path: str) -> str:
    converted_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex[:12]}.gif")
    await asyncio.to_thread(apnggif, path, converted_path)
    stack.callback(safe_delete, converted_path)
    return converted_path


async def hash_file(path: str) -> str | None:
    if not os.path.exists(path):
        return None

    def _compute() -> str:
        import hashlib

        hasher = hashlib.sha256()
        with open(path, "rb") as file_obj:
            while True:
                chunk = file_obj.read(1024 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)
        return hasher.hexdigest()

    try:
        return await asyncio.to_thread(_compute)
    except Exception:
        return None


async def build_evidence_file(path: str, item: MediaWorkItem) -> discord.File | None:
    if not os.path.exists(path):
        return None

    def _open():
        return open(path, "rb")

    try:
        fp = await asyncio.to_thread(_open)
    except Exception:
        return None
    filename = resolve_filename(item, path)
    return discord.File(fp, filename=filename)


def resolve_filename(item: MediaWorkItem, fallback_path: str) -> str:
    raw_label = item.label or ""
    parsed = urlparse(raw_label)
    candidate = os.path.basename(parsed.path)
    if candidate:
        return candidate
    return os.path.basename(fallback_path)


__all__ = [
    "convert_apng",
    "hash_file",
    "build_evidence_file",
    "resolve_filename",
]
