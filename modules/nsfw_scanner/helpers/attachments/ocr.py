from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from typing import Any

import discord

from modules.nsfw_scanner.constants import TMP_DIR
from modules.nsfw_scanner.helpers.ocr import extract_text_from_image as default_extract_text_from_image
from modules.nsfw_scanner.utils.file_ops import safe_delete

from .cache import AttachmentSettingsCache

__all__ = [
    "duplicate_file_for_ocr",
    "schedule_async_ocr_text_scan",
    "wait_for_async_ocr_tasks",
]

_OCR_TASKS: set[asyncio.Task[Any]] = set()
_ASYNC_OCR_SEMAPHORE: asyncio.Semaphore | None = None


def _get_async_ocr_semaphore() -> asyncio.Semaphore:
    global _ASYNC_OCR_SEMAPHORE
    if _ASYNC_OCR_SEMAPHORE is None:
        try:
            concurrency = int(os.getenv("NSFW_ASYNC_OCR_CONCURRENCY", "4"))
        except ValueError:
            concurrency = 4
        _ASYNC_OCR_SEMAPHORE = asyncio.Semaphore(max(1, concurrency))
    return _ASYNC_OCR_SEMAPHORE


async def duplicate_file_for_ocr(source_path: str) -> str | None:
    suffix = os.path.splitext(source_path)[1] or ".tmp"
    temp_name = f"ocr_{uuid.uuid4().hex}{suffix}"
    dest_path = os.path.join(TMP_DIR, temp_name)
    try:
        await asyncio.to_thread(shutil.copyfile, source_path, dest_path)
    except Exception as exc:  # pragma: no cover - best effort logging
        print(f"[ocr] Failed to stage file for OCR {source_path}: {exc}")
        safe_delete(dest_path)
        return None
    return dest_path


def _track_background_task(task: asyncio.Task[Any]) -> None:
    _OCR_TASKS.add(task)

    def _finalizer(completed: asyncio.Task[Any]) -> None:
        _OCR_TASKS.discard(completed)
        try:
            completed.result()
        except Exception as exc:  # pragma: no cover - best effort logging
            print(f"[ocr] Async OCR task failed: {exc}")

    task.add_done_callback(_finalizer)


async def _run_async_ocr_text_scan(
    *,
    scanner,
    temp_path: str,
    languages: list[str],
    text_pipeline,
    guild_id: int,
    message,
    nsfw_callback,
    settings_map: dict[str, Any] | None,
    metadata_overrides: dict[str, Any] | None,
    queue_name: str | None,
    perform_actions: bool,
    accelerated: bool,
    extract_text_fn,
) -> None:
    semaphore = _get_async_ocr_semaphore()
    extracted_text: str | None = None
    try:
        async with semaphore:
            extracted_text = await extract_text_fn(
                temp_path,
                languages=languages,
            )
    except Exception as exc:  # pragma: no cover - best effort logging
        print(f"[ocr] Async OCR extraction failed: {exc}")
    if not extracted_text:
        safe_delete(temp_path)
        return

    cache = AttachmentSettingsCache()
    cache.set_accelerated(accelerated)
    cache.set_text_enabled(True)

    metadata = dict(metadata_overrides or {})
    evidence_filename = metadata.get("filename") or os.path.basename(temp_path)

    callback_for_scan = nsfw_callback if perform_actions else None
    if callback_for_scan is not None:
        original_callback = callback_for_scan

        async def _ocr_callback(user, bot_obj, callback_guild_id, reason, image, callback_message, **kwargs):
            patched_kwargs = dict(kwargs)
            patched_kwargs["send_embed"] = True
            if extracted_text:
                patched_kwargs["detected_text"] = extracted_text
            evidence_file = None
            final_image = image
            if os.path.exists(temp_path):
                try:
                    evidence_file = discord.File(temp_path, filename=evidence_filename)
                    final_image = evidence_file
                except Exception:
                    evidence_file = None
                    final_image = image
            try:
                return await original_callback(
                    user,
                    bot_obj,
                    callback_guild_id,
                    reason,
                    final_image,
                    callback_message,
                    **patched_kwargs,
                )
            finally:
                if evidence_file is not None:
                    try:
                        evidence_file.close()
                    except Exception:
                        pass

        callback_for_scan = _ocr_callback

    try:
        await text_pipeline.scan(
            scanner=scanner,
            message=message,
            guild_id=guild_id,
            nsfw_callback=callback_for_scan,
            settings_cache=cache,
            settings_map=settings_map,
            text_override=extracted_text,
            source_hint="Image OCR",
            metadata_overrides=metadata,
            queue_label=queue_name,
        )
    except Exception as exc:  # pragma: no cover - best effort logging
        print(f"[ocr] Async OCR text scan failed: {exc}")
    finally:
        safe_delete(temp_path)


def schedule_async_ocr_text_scan(*, extract_text_fn=None, **kwargs) -> None:
    task = asyncio.create_task(
        _run_async_ocr_text_scan(
            extract_text_fn=extract_text_fn or default_extract_text_from_image,
            **kwargs,
        )
    )
    _track_background_task(task)


async def wait_for_async_ocr_tasks() -> None:
    pending = list(_OCR_TASKS)
    if not pending:
        return
    await asyncio.gather(*pending, return_exceptions=True)
