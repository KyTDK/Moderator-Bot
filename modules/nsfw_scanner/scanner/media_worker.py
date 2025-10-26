from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
import uuid
from contextlib import AsyncExitStack
from typing import Any, Optional
from urllib.parse import urlparse

import discord
from apnggif import apnggif

from ..cache import verdict_cache
from ..constants import TMP_DIR
from ..context import GuildScanContext
from ..helpers.downloads import DownloadResult, temp_download
from ..helpers.images import process_image
from ..helpers.videos import process_video
from ..reporting import dispatch_callback, emit_verbose_report
from ..utils.file_ops import safe_delete
from ..utils.file_types import FILE_TYPE_IMAGE, FILE_TYPE_VIDEO, determine_file_type
from .metrics import queue_media_metrics
from .work_item import MediaFlagged, MediaWorkItem

log = logging.getLogger(__name__)


def _clone_scan_result(result: dict[str, Any]) -> dict[str, Any]:
    cloned = dict(result)
    metrics = cloned.get("pipeline_metrics")
    if isinstance(metrics, dict):
        cloned["pipeline_metrics"] = dict(metrics)
    return cloned


def _annotate_cache_status(result: dict[str, Any] | None, status: str | None) -> dict[str, Any] | None:
    if result is None or not status:
        return result
    result["cache_status"] = status
    metrics = result.get("pipeline_metrics")
    if isinstance(metrics, dict):
        metrics = dict(metrics)
        metrics["cache_status"] = status
        result["pipeline_metrics"] = metrics
    else:
        result["pipeline_metrics"] = {"cache_status": status}
    return result


async def scan_media_item(
    scanner,
    *,
    item: MediaWorkItem,
    context: GuildScanContext,
    message: discord.Message | None,
    actor: discord.Member | None,
    nsfw_callback,
) -> None:
    started_at = time.perf_counter()
    cache_tokens: list[tuple[str, object | None]] = []
    reuse_verdict: dict[str, Any] | None = None
    reuse_status: str | None = None

    initial_reservation = await verdict_cache.claim(item.cache_key)
    if initial_reservation.verdict is not None:
        if bool(initial_reservation.verdict.get("is_nsfw")):
            reuse_verdict = _annotate_cache_status(
                _clone_scan_result(initial_reservation.verdict),
                "cache_hit_nsfw",
            )
            reuse_status = "cache_hit_nsfw"
        else:
            cached_result = _annotate_cache_status(
                _clone_scan_result(initial_reservation.verdict),
                "cache_hit_safe",
            )
            await _emit_verbose_if_needed(
                scanner,
                context=context,
                message=message,
                actor=actor,
                scan_result=cached_result,
                file_type=None,
                detected_mime=None,
                duration_ms=int((time.perf_counter() - started_at) * 1000),
            )
            _queue_metrics(
                context=context,
                message=message,
                actor=actor,
                item=item,
                duration_ms=int((time.perf_counter() - started_at) * 1000),
                result=initial_reservation.verdict,
                detected_mime=None,
                file_type=None,
                status="cache_hit_safe",
            )
            return
    elif initial_reservation.waiter is not None:
        verdict = await initial_reservation.waiter
        if verdict and verdict.get("is_nsfw"):
            reuse_verdict = _annotate_cache_status(
                _clone_scan_result(verdict),
                "cache_shared_nsfw",
            )
            reuse_status = "cache_shared_nsfw"
        else:
            cached_result = _annotate_cache_status(
                _clone_scan_result(verdict) if verdict else None,
                "cache_shared_safe",
            )
            await _emit_verbose_if_needed(
                scanner,
                context=context,
                message=message,
                actor=actor,
                scan_result=cached_result,
                file_type=None,
                detected_mime=None,
                duration_ms=int((time.perf_counter() - started_at) * 1000),
            )
            _queue_metrics(
                context=context,
                message=message,
                actor=actor,
                item=item,
                duration_ms=int((time.perf_counter() - started_at) * 1000),
                result=verdict,
                detected_mime=None,
                file_type=None,
                status="cache_shared_safe",
            )
            return
    else:
        cache_tokens.append((item.cache_key, initial_reservation.token))

    try:
        async with AsyncExitStack() as stack:
            download = await stack.enter_async_context(
                temp_download(
                    scanner.session,
                    item.url,
                    guild_key=_guild_key(context),
                    limits=context.limits,
                    ext=item.ext_hint,
                    prefer_video=item.prefer_video,
                    head_cache=context.head_cache,
                )
            )
            prepared_path = download.path
            if item.metadata.get("sticker_format") == "apng":
                prepared_path = await _convert_apng(stack, prepared_path)

            file_type, detected_mime = determine_file_type(prepared_path)

            sha_key = None
            file_hash = await _hash_file(prepared_path)
            if file_hash:
                sha_key = f"sha256::{file_hash}"
                sha_reservation = await verdict_cache.claim(sha_key)
                if sha_reservation.verdict is not None:
                    if bool(sha_reservation.verdict.get("is_nsfw")):
                        reuse_verdict = _annotate_cache_status(
                            _clone_scan_result(sha_reservation.verdict),
                            reuse_status or "cache_hash_nsfw",
                        )
                        reuse_status = reuse_status or "cache_hash_nsfw"
                    else:
                        cached_result = _annotate_cache_status(
                            _clone_scan_result(sha_reservation.verdict),
                            "cache_hash_safe",
                        )
                        await _emit_verbose_if_needed(
                            scanner,
                            context=context,
                            message=message,
                            actor=actor,
                            scan_result=cached_result,
                            file_type=file_type,
                            detected_mime=detected_mime,
                            duration_ms=int((time.perf_counter() - started_at) * 1000),
                        )
                        _queue_metrics(
                            context=context,
                            message=message,
                            actor=actor,
                            item=item,
                            duration_ms=int((time.perf_counter() - started_at) * 1000),
                            result=sha_reservation.verdict,
                            detected_mime=detected_mime,
                            file_type=file_type,
                            status="cache_hash_safe",
                        )
                        return
                elif sha_reservation.waiter is not None:
                    verdict = await sha_reservation.waiter
                    if verdict and verdict.get("is_nsfw"):
                        reuse_verdict = _annotate_cache_status(
                            _clone_scan_result(verdict),
                            reuse_status or "cache_hash_shared_nsfw",
                        )
                        reuse_status = reuse_status or "cache_hash_shared_nsfw"
                    else:
                        cached_result = _annotate_cache_status(
                            _clone_scan_result(verdict) if verdict else None,
                            "cache_hash_shared_safe",
                        )
                        await _emit_verbose_if_needed(
                            scanner,
                            context=context,
                            message=message,
                            actor=actor,
                            scan_result=cached_result,
                            file_type=file_type,
                            detected_mime=detected_mime,
                            duration_ms=int((time.perf_counter() - started_at) * 1000),
                        )
                        _queue_metrics(
                            context=context,
                            message=message,
                            actor=actor,
                            item=item,
                            duration_ms=int((time.perf_counter() - started_at) * 1000),
                            result=verdict,
                            detected_mime=detected_mime,
                            file_type=file_type,
                            status="cache_hash_shared_safe",
                        )
                        return
                else:
                    cache_tokens.append((sha_key, sha_reservation.token))

            scan_result: dict[str, Any] | None = reuse_verdict
            evidence_file: Optional[discord.File] = None
            video_attachment: Optional[discord.File] = None

            if scan_result is None:
                if file_type == FILE_TYPE_IMAGE:
                    scan_result = await process_image(
                        scanner,
                        original_filename=prepared_path,
                        guild_id=context.guild_id,
                        clean_up=False,
                        context=context.image_context,
                    )
                elif file_type == FILE_TYPE_VIDEO:
                    video_attachment, scan_result = await process_video(
                        scanner,
                        original_filename=prepared_path,
                        guild_id=context.guild_id,
                        context=context.image_context,
                        premium_status=context.premium_status,
                    )
                else:
                    _queue_metrics(
                        context=context,
                        message=message,
                        actor=actor,
                        item=item,
                        duration_ms=int((time.perf_counter() - started_at) * 1000),
                        result=None,
                        detected_mime=detected_mime,
                        file_type=file_type,
                        status="unsupported_type",
                    )
                    return

            for cache_key, token in cache_tokens:
                if cache_key:
                    await verdict_cache.resolve(cache_key, token, scan_result or {})

            duration_ms = int(max((time.perf_counter() - started_at) * 1000, 0))
            status = reuse_status or "scan_complete"
            _queue_metrics(
                context=context,
                message=message,
                actor=actor,
                item=item,
                duration_ms=duration_ms,
                result=scan_result,
                detected_mime=detected_mime,
                file_type=file_type,
                status=status,
                download=download,
            )

            await _emit_verbose_if_needed(
                scanner,
                context=context,
                message=message,
                actor=actor,
                scan_result=scan_result,
                file_type=file_type,
                detected_mime=detected_mime,
                duration_ms=duration_ms,
                cache_status=reuse_status,
            )

            if scan_result and scan_result.get("is_nsfw"):
                evidence_file = video_attachment or await _build_evidence_file(prepared_path, item)
                if evidence_file is not None and message is not None:
                    await dispatch_callback(
                        scanner=scanner,
                        nsfw_callback=nsfw_callback,
                        author=actor,
                        guild_id=context.guild_id or 0,
                        scan_result=scan_result,
                        message=message,
                        file=evidence_file,
                    )
                raise MediaFlagged(scan_result or {})
            if video_attachment:
                try:
                    video_attachment.close()
                except Exception:
                    pass
    except ValueError as download_error:
        log.debug("Skipping media %s due to download restriction: %s", item.url, download_error)
    except MediaFlagged:
        raise
    except Exception as exc:
        log.exception("Failed to scan media %s: %s", item.url, exc)


def _queue_metrics(**kwargs) -> None:
    queue_media_metrics(**kwargs)


def _guild_key(context: GuildScanContext) -> str:
    if context.guild_id is not None:
        return str(context.guild_id)
    return f"global-{context.plan}"


async def _convert_apng(stack: AsyncExitStack, path: str) -> str:
    converted_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex[:12]}.gif")
    await asyncio.to_thread(apnggif, path, converted_path)
    stack.callback(safe_delete, converted_path)
    return converted_path


async def _hash_file(path: str) -> str | None:
    if not os.path.exists(path):
        return None

    def _compute() -> str:
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


async def _build_evidence_file(path: str, item: MediaWorkItem) -> discord.File | None:
    if not os.path.exists(path):
        return None

    def _open():
        return open(path, "rb")

    try:
        fp = await asyncio.to_thread(_open)
    except Exception:
        return None
    filename = _resolve_filename(item, path)
    return discord.File(fp, filename=filename)


async def _emit_verbose_if_needed(
    scanner,
    *,
    context: GuildScanContext,
    message: discord.Message | None,
    actor,
    scan_result: dict[str, Any] | None,
    file_type: str | None,
    detected_mime: str | None,
    duration_ms: int,
    cache_status: str | None = None,
) -> None:
    if not (context.nsfw_verbose and message is not None and scan_result is not None):
        return
    payload = _annotate_cache_status(_clone_scan_result(scan_result), cache_status)
    await emit_verbose_report(
        scanner,
        message=message,
        author=actor,
        guild_id=context.guild_id,
        file_type=file_type,
        detected_mime=detected_mime,
        scan_result=payload,
        duration_ms=duration_ms,
    )


def _resolve_filename(item: MediaWorkItem, fallback_path: str) -> str:
    raw_label = item.label or ""
    parsed = urlparse(raw_label)
    candidate = os.path.basename(parsed.path)
    if candidate:
        return candidate
    return os.path.basename(fallback_path)


__all__ = ["scan_media_item", "MediaFlagged"]
