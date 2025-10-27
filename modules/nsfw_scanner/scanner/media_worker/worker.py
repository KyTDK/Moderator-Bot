from __future__ import annotations

import logging
import os
import tempfile
import time
from collections.abc import Iterable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, Optional
from urllib.parse import urlparse

import aiohttp
import discord
from discord.utils import utcnow

from ...cache import verdict_cache
from ...constants import LOG_CHANNEL_ID
from ...context import GuildScanContext
from ...helpers.downloads import DownloadResult
from ...helpers.images import process_image
from ...helpers.videos import process_video
from ...reporting import dispatch_callback
from ...utils.file_types import FILE_TYPE_IMAGE, FILE_TYPE_VIDEO, determine_file_type
from ...utils.file_ops import safe_delete
from ..work_item import MediaFlagged, MediaWorkItem
from .cache import annotate_cache_status, clone_scan_result
from .diagnostics import (
    emit_verbose_if_needed,
    notify_download_failure,
    should_emit_diagnostic,
)
from .files import build_evidence_file, convert_apng, hash_file
from modules.utils.log_channel import send_log_message

log = logging.getLogger(__name__)


_CHUNK_SIZE = 1 << 17


def _ensure_tmp_dir(scanner) -> None:
    os.makedirs(scanner.tmp_dir, exist_ok=True)


def _resolve_suffix(candidate: str | None, fallback: str) -> str:
    suffix = candidate or fallback
    if suffix and not suffix.startswith("."):
        suffix = f".{suffix}"
    return suffix or fallback


def _normalise_url_list(*values: object) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    def _register(value: str | None) -> None:
        if not value:
            return
        candidate = str(value).strip()
        if not candidate:
            return
        if candidate in seen:
            return
        seen.add(candidate)
        urls.append(candidate)

    for value in values:
        if not value:
            continue
        if isinstance(value, str):
            _register(value)
            continue
        if isinstance(value, Iterable):
            for inner in value:
                _register(inner)
            continue
        _register(str(value))

    return urls


@asynccontextmanager
async def _attachment_to_tempfile(scanner, item: MediaWorkItem) -> Any:
    attachment = item.attachment
    if attachment is None:
        raise RuntimeError("MediaWorkItem missing attachment reference")
    _ensure_tmp_dir(scanner)
    suffix = _resolve_suffix(item.ext_hint, os.path.splitext(getattr(attachment, "filename", "") or "")[1] or ".bin")
    fd, path = tempfile.mkstemp(dir=scanner.tmp_dir, suffix=suffix)
    os.close(fd)
    try:
        await attachment.save(path)
        size = getattr(attachment, "size", None)
        if size is None:
            try:
                size = os.path.getsize(path)
            except OSError:
                size = None
        url_value = (
            getattr(attachment, "url", None)
            or getattr(attachment, "proxy_url", None)
            or item.url
            or item.label
        )
        content_type = getattr(attachment, "content_type", None)
        yield DownloadResult(
            path,
            url=str(url_value) if url_value else item.label,
            content_type=content_type,
            bytes_downloaded=size,
        )
    finally:
        safe_delete(path)


@asynccontextmanager
async def _download_url_exact(
    scanner,
    item: MediaWorkItem,
    *,
    limits,
    url_override: str | None = None,
) -> Any:
    target_url = url_override or item.url
    if not target_url:
        raise RuntimeError("MediaWorkItem missing URL for download")
    if scanner.session is None:
        raise RuntimeError("NSFWScanner session is not initialised. Call start() first.")
    _ensure_tmp_dir(scanner)
    parsed = urlparse(target_url)
    suffix = _resolve_suffix(item.ext_hint, os.path.splitext(parsed.path)[1] or ".bin")
    fd, path = tempfile.mkstemp(dir=scanner.tmp_dir, suffix=suffix)
    os.close(fd)
    total = 0
    content_type: str | None = None
    resolved_url: str | None = None
    try:
        async with scanner.session.get(target_url) as resp:
            resp.raise_for_status()
            content_type = resp.headers.get("Content-Type")
            content_length = resp.content_length
            cap = limits.download_cap_bytes
            if cap is not None and content_length and content_length > cap:
                raise ValueError(f"Download exceeds cap ({content_length} bytes)")
            resolved_url = str(resp.url) if getattr(resp, "url", None) else target_url
            with open(path, "wb") as handle:
                async for chunk in resp.content.iter_chunked(_CHUNK_SIZE):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if cap is not None and total > cap:
                        raise ValueError("Download exceeds cap")
                    handle.write(chunk)
        if total == 0:
            try:
                total = os.path.getsize(path)
            except OSError:
                total = 0
        yield DownloadResult(
            path,
            url=resolved_url or target_url,
            content_type=content_type,
            bytes_downloaded=total or None,
        )
    finally:
        safe_delete(path)


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
    diagnostic_key_base = f"{context.guild_id or 'global'}::{item.source or 'unknown'}"

    async def _emit_diagnostic(
        reason: str,
        *,
        status: str | None = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        if not LOG_CHANNEL_ID:
            return
        bot = getattr(scanner, "bot", None)
        if bot is None:
            return
        throttle_key = f"{diagnostic_key_base}::{reason}"
        if not should_emit_diagnostic(throttle_key):
            return
        embed = discord.Embed(
            title="NSFW scan skipped",
            description=item.label or item.url or "Unknown media item",
            color=discord.Color.orange(),
            timestamp=utcnow(),
        )
        embed.add_field(name="Reason", value=reason, inline=True)
        if status:
            embed.add_field(name="Status", value=status, inline=True)
        embed.add_field(name="Source", value=item.source or "unknown", inline=True)

        metadata = item.metadata or {}
        context_lines = []
        guild_id = metadata.get("guild_id") or context.guild_id
        channel_id = metadata.get("channel_id")
        message_id = metadata.get("message_id")
        attachment_id = metadata.get("attachment_id")
        if guild_id is not None:
            context_lines.append(f"Guild: {guild_id}")
        if channel_id is not None:
            context_lines.append(f"Channel: {channel_id}")
        if message_id is not None:
            context_lines.append(f"Message: {message_id}")
        if attachment_id is not None:
            context_lines.append(f"Attachment: {attachment_id}")
        if context_lines:
            embed.add_field(name="Context", value="\n".join(context_lines), inline=False)

        if extra:
            detail_lines: list[str] = []
            for key, value in extra.items():
                if value is None:
                    continue
                detail_lines.append(f"{key}: {value}")
            if detail_lines:
                detail_text = "\n".join(detail_lines)
                if len(detail_text) > 1024:
                    detail_text = f"{detail_text[:1021]}…"
                embed.add_field(name="Details", value=detail_text, inline=False)

        if message is not None and getattr(message, "jump_url", None):
            embed.add_field(name="Message Link", value=message.jump_url, inline=False)

        if item.url:
            url_value = item.url
            if len(url_value) > 1024:
                url_value = f"{url_value[:1021]}…"
            embed.add_field(name="URL", value=url_value, inline=False)
        success = await send_log_message(
            bot,
            embed=embed,
            logger=log,
            context="nsfw_diagnostic",
        )
        if not success:  # pragma: no cover - best effort logging
            log.debug(
                "Failed to send NSFW diagnostic to channel %s", LOG_CHANNEL_ID, exc_info=True
            )

    async def _resolve_cache_tokens(verdict: dict[str, Any]) -> None:
        if not cache_tokens:
            return
        for cache_key, token in cache_tokens:
            if not cache_key or token is None:
                continue
            try:
                await verdict_cache.resolve(cache_key, token, verdict)
            except Exception:
                log.debug("Failed to resolve cache token %s", cache_key, exc_info=True)
        cache_tokens.clear()

    async def _resolve_skip(
        reason: str,
        extra: Optional[dict[str, Any]] = None,
        *,
        status: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "is_nsfw": False,
            "skipped": True,
            "skip_reason": reason,
        }
        if extra:
            payload.update(extra)
        await _resolve_cache_tokens(payload)
        should_emit = reason != "unsupported_type"
        if should_emit:
            await _emit_diagnostic(reason, status=status or reason, extra=extra)
        return payload

    initial_reservation = await verdict_cache.claim(item.cache_key)
    if initial_reservation.verdict is not None:
        if bool(initial_reservation.verdict.get("is_nsfw")):
            reuse_verdict = annotate_cache_status(
                clone_scan_result(initial_reservation.verdict),
                "cache_hit_nsfw",
            )
            reuse_status = "cache_hit_nsfw"
        else:
            cached_result = annotate_cache_status(
                clone_scan_result(initial_reservation.verdict),
                "cache_hit_safe",
            )
            await emit_verbose_if_needed(
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
            reuse_verdict = annotate_cache_status(
                clone_scan_result(verdict),
                "cache_shared_nsfw",
            )
            reuse_status = "cache_shared_nsfw"
        else:
            cached_result = annotate_cache_status(
                clone_scan_result(verdict) if verdict else None,
                "cache_shared_safe",
            )
            await emit_verbose_if_needed(
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

    metadata = item.metadata or {}
    fallback_urls: list[str] = _normalise_url_list(
        metadata.get("fallback_urls"),
        metadata.get("fallback_url"),
    )
    candidate_urls_meta: list[str] = _normalise_url_list(
        metadata.get("candidate_urls"),
        metadata.get("candidate_url"),
    )
    refreshed_urls: list[str] = _normalise_url_list(
        metadata.get("refreshed_urls"),
        metadata.get("refreshed_url"),
    )
    attempted_urls: list[str] = []

    try:
        async def _process_download(download_obj: DownloadResult) -> None:
            nonlocal reuse_verdict, reuse_status
            prepared_path = download_obj.path
            async with AsyncExitStack() as stack:
                if item.metadata.get("sticker_format") == "apng":
                    prepared_path = await convert_apng(stack, prepared_path)

                file_type, detected_mime = determine_file_type(prepared_path)

                sha_key = None
                file_hash = await hash_file(prepared_path)
                if file_hash:
                    sha_key = f"sha256::{file_hash}"
                    sha_reservation = await verdict_cache.claim(sha_key)
                    if sha_reservation.verdict is not None:
                        if bool(sha_reservation.verdict.get("is_nsfw")):
                            reuse_verdict = annotate_cache_status(
                                clone_scan_result(sha_reservation.verdict),
                                reuse_status or "cache_hash_nsfw",
                            )
                            reuse_status = reuse_status or "cache_hash_nsfw"
                        else:
                            cached_result = annotate_cache_status(
                                clone_scan_result(sha_reservation.verdict),
                                "cache_hash_safe",
                            )
                            await emit_verbose_if_needed(
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
                            reuse_verdict = annotate_cache_status(
                                clone_scan_result(verdict),
                                reuse_status or "cache_hash_shared_nsfw",
                            )
                            reuse_status = reuse_status or "cache_hash_shared_nsfw"
                        else:
                            cached_result = annotate_cache_status(
                                clone_scan_result(verdict) if verdict else None,
                                "cache_hash_shared_safe",
                            )
                            await emit_verbose_if_needed(
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
                        await _resolve_skip(
                            "unsupported_type",
                            {"detected_mime": detected_mime or "unknown"},
                            status="unsupported_type",
                        )
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

                await _resolve_cache_tokens(scan_result or {})

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
                    download=download_obj,
                )

                await emit_verbose_if_needed(
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
                    evidence_file = video_attachment or await build_evidence_file(prepared_path, item)
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

        if item.attachment is not None:
            async with _attachment_to_tempfile(scanner, item) as temp_download:
                await _process_download(temp_download)
            return

        if item.url:
            url_candidates: list[str] = []
            seen_candidates: set[str] = set()

            def _queue_candidate(value: str | None) -> None:
                if not value:
                    return
                candidate = str(value).strip()
                if not candidate or candidate in seen_candidates:
                    return
                seen_candidates.add(candidate)
                url_candidates.append(candidate)

            _queue_candidate(item.url)
            for candidate in candidate_urls_meta:
                _queue_candidate(candidate)
            for candidate in fallback_urls:
                _queue_candidate(candidate)
            for candidate in refreshed_urls:
                _queue_candidate(candidate)

            attempted_set: set[str] = set()
            observed_refreshed: list[str] = []
            last_http_error: aiohttp.ClientResponseError | None = None

            for candidate_url in url_candidates:
                try:
                    async with _download_url_exact(
                        scanner,
                        item,
                        limits=context.limits,
                        url_override=candidate_url,
                    ) as temp_download:
                        if candidate_url not in attempted_set:
                            attempted_urls.append(candidate_url)
                            attempted_set.add(candidate_url)
                        resolved_url = getattr(temp_download, "url", None)
                        if (
                            resolved_url
                            and isinstance(resolved_url, str)
                            and resolved_url not in attempted_set
                            and resolved_url not in refreshed_urls
                            and resolved_url not in observed_refreshed
                        ):
                            observed_refreshed.append(resolved_url)
                        await _process_download(temp_download)
                        return
                except aiohttp.ClientResponseError as http_error:
                    if candidate_url not in attempted_set:
                        attempted_urls.append(candidate_url)
                        attempted_set.add(candidate_url)
                    request_info = getattr(http_error, "request_info", None)
                    real_url = None
                    if request_info is not None and getattr(request_info, "real_url", None):
                        real_url = str(request_info.real_url)
                    if (
                        real_url
                        and real_url not in attempted_set
                        and real_url not in refreshed_urls
                        and real_url not in observed_refreshed
                    ):
                        observed_refreshed.append(real_url)
                    last_http_error = http_error
                    continue

            if observed_refreshed:
                refreshed_urls.extend(
                    url for url in observed_refreshed if url not in refreshed_urls
                )

            if last_http_error is not None:
                setattr(last_http_error, "_attempted_urls", list(attempted_urls))
                setattr(last_http_error, "_fallback_urls", list(fallback_urls))
                setattr(last_http_error, "_refreshed_urls", list(refreshed_urls))
                raise last_http_error
            return

        await _resolve_skip(
            "missing_source",
            {"error": "no attachment or url available"},
            status="missing_source",
        )
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        _queue_metrics(
            context=context,
            message=message,
            actor=actor,
            item=item,
            duration_ms=duration_ms,
            result=None,
            detected_mime=None,
            file_type=None,
            status="missing_source",
            download=None,
        )
        return
    except ValueError as download_error:
        await _resolve_skip(
            "download_restricted",
            {"error": str(download_error)},
            status="download_restricted",
        )
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        log.debug(
            "Skipping media %s due to download restriction: %s", item.url, download_error
        )
        _queue_metrics(
            context=context,
            message=message,
            actor=actor,
            item=item,
            duration_ms=duration_ms,
            result=None,
            detected_mime=None,
            file_type=None,
            status="download_restricted",
            download=None,
        )
    except aiohttp.ClientResponseError as http_error:
        attempted_for_log = getattr(http_error, "_attempted_urls", attempted_urls)
        fallback_for_log = getattr(http_error, "_fallback_urls", fallback_urls)
        refreshed_for_log = getattr(http_error, "_refreshed_urls", refreshed_urls)
        if not getattr(http_error, "_download_failure_logged", False):
            await notify_download_failure(
                scanner,
                item=item,
                context=context,
                message=message,
                attempted_urls=attempted_for_log or ([item.url] if item.url else []),
                fallback_urls=fallback_for_log,
                refreshed_urls=refreshed_for_log,
                error=http_error,
                logger=log,
            )
            setattr(http_error, "_download_failure_logged", True)
        await _resolve_skip(
            "http_error",
            {
                "error_status": http_error.status,
                "error_message": http_error.message,
                "attempted_urls": ", ".join(attempted_for_log or []) or item.url,
                "fallback_urls": ", ".join(fallback_for_log or []),
            },
            status=f"http_error_{http_error.status}",
        )
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        failed_url = item.url
        request_info = getattr(http_error, "request_info", None)
        if request_info is not None and getattr(request_info, "real_url", None) is not None:
            failed_url = str(request_info.real_url)
        log.warning(
            "Failed to download media %s (HTTP %s): %s",
            failed_url,
            http_error.status,
            http_error.message,
        )
        _queue_metrics(
            context=context,
            message=message,
            actor=actor,
            item=item,
            duration_ms=duration_ms,
            result=None,
            detected_mime=None,
            file_type=None,
            status=f"http_error_{http_error.status}",
            download=None,
        )
    except MediaFlagged:
        raise
    except Exception as exc:
        await _resolve_skip("exception", {"error": repr(exc)}, status="exception")
        log.exception("Failed to scan media %s: %s", item.url, exc)
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        _queue_metrics(
            context=context,
            message=message,
            actor=actor,
            item=item,
            duration_ms=duration_ms,
            result=None,
            detected_mime=None,
            file_type=None,
            status="exception",
            download=None,
        )


def _queue_metrics(**kwargs) -> None:
    from ..metrics import queue_media_metrics

    queue_media_metrics(**kwargs)


def _guild_key(context: GuildScanContext) -> str:
    if context.guild_id is not None:
        return str(context.guild_id)
    return f"global-{context.plan}"


__all__ = ["scan_media_item"]
