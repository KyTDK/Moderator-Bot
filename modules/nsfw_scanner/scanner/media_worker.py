from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
import uuid
from contextlib import AsyncExitStack
from typing import Any, Optional
from urllib.parse import quote, urlparse, urlunparse

import discord
import aiohttp
from apnggif import apnggif
from discord.utils import utcnow

from ..cache import verdict_cache
from ..constants import TMP_DIR, LOG_CHANNEL_ID
from ..context import GuildScanContext
from ..helpers.downloads import DownloadResult, temp_download
from ..helpers.images import process_image
from ..helpers.videos import process_video
from ..reporting import dispatch_callback, emit_verbose_report
from ..utils.file_ops import safe_delete
from ..utils.file_types import FILE_TYPE_IMAGE, FILE_TYPE_VIDEO, determine_file_type
from .metrics import queue_media_metrics
from .work_item import MediaFlagged, MediaWorkItem
from modules.utils.discord_utils import safe_get_channel
from modules.utils.log_channel import send_log_message

log = logging.getLogger(__name__)

_DIAGNOSTIC_THROTTLE: dict[str, float] = {}
_DIAGNOSTIC_COOLDOWN_SECONDS = 120.0


def _should_emit_diagnostic(key: str) -> bool:
    now = time.monotonic()
    last = _DIAGNOSTIC_THROTTLE.get(key)
    if last is not None and (now - last) < _DIAGNOSTIC_COOLDOWN_SECONDS:
        return False
    _DIAGNOSTIC_THROTTLE[key] = now
    return True


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


async def _resolve_attachment_refresh_candidates(
    scanner,
    *,
    item: MediaWorkItem,
    message: discord.Message | None,
) -> list[str]:
    metadata = item.metadata or {}
    attachment_id = metadata.get("attachment_id")
    channel_id = metadata.get("channel_id")
    message_id = metadata.get("message_id")
    if not attachment_id or not channel_id or not message_id:
        return []

    try:
        channel_id_int = int(channel_id)
        message_id_int = int(message_id)
        attachment_id_int = int(attachment_id)
    except (TypeError, ValueError):
        return []

    channel_obj = getattr(message, "channel", None)
    if channel_obj is None or getattr(channel_obj, "id", None) != channel_id_int:
        channel_obj = await safe_get_channel(scanner.bot, channel_id_int)

    if channel_obj is None or not hasattr(channel_obj, "fetch_message"):
        return []

    try:
        refreshed_message = await channel_obj.fetch_message(message_id_int)
    except (discord.NotFound, discord.Forbidden):
        return []
    except discord.HTTPException:
        return []

    refreshed_urls: list[str] = []
    for attachment in getattr(refreshed_message, "attachments", ()): 
        if getattr(attachment, "id", None) == attachment_id_int:
            for candidate in (
                getattr(attachment, "proxy_url", None),
                getattr(attachment, "url", None),
            ):
                if not candidate or candidate in refreshed_urls:
                    continue
                parsed_candidate = urlparse(candidate)
                if parsed_candidate.scheme not in {"http", "https"}:
                    continue
                refreshed_urls.append(candidate)
            break
    return refreshed_urls


def _normalise_candidate_url(item: MediaWorkItem, candidate: str | None) -> str | None:
    if not isinstance(candidate, str):
        return None

    stripped = candidate.strip()
    if not stripped:
        return None

    parsed = urlparse(stripped)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return stripped

    if not parsed.scheme and parsed.netloc:
        return urlunparse(("https", parsed.netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))

    if not parsed.scheme and parsed.path.startswith("/attachments/"):
        return urlunparse(("https", "cdn.discordapp.com", parsed.path, "", "", ""))

    if not parsed.scheme and parsed.path.startswith("attachments/"):
        path = f"/{parsed.path}"
        return urlunparse(("https", "cdn.discordapp.com", path, "", "", ""))

    metadata = item.metadata or {}
    attachment_id = metadata.get("attachment_id")
    channel_id = metadata.get("channel_id")

    if not attachment_id or not channel_id:
        return None

    try:
        attachment_id_int = int(attachment_id)
        channel_id_int = int(channel_id)
    except (TypeError, ValueError):
        return None

    filename = parsed.path or ""
    if not filename:
        filename = metadata.get("filename") or item.label or ""
    filename = filename.strip().lstrip("/")
    if not filename:
        return None

    safe_filename = quote(filename)
    path = f"/attachments/{channel_id_int}/{attachment_id_int}/{safe_filename}"
    return urlunparse(("https", "cdn.discordapp.com", path, "", "", ""))


def _suppress_discord_link_embed(url: str) -> str:
    if not isinstance(url, str):
        return url
    stripped = url.strip()
    if not stripped:
        return stripped
    if stripped.startswith("<") and stripped.endswith(">"):
        return stripped
    scheme = urlparse(stripped).scheme
    if scheme in {"http", "https"}:
        return f"<{stripped}>"
    return stripped


async def _notify_download_failure(
    scanner,
    *,
    item: MediaWorkItem,
    context: GuildScanContext,
    message: discord.Message | None,
    attempted_urls: list[str],
    fallback_urls: list[str],
    error: aiohttp.ClientResponseError,
) -> None:
    if not LOG_CHANNEL_ID:
        return
    bot = getattr(scanner, "bot", None)
    if bot is None:
        return

    metadata = item.metadata or {}
    attempted_display = "\n".join(
        _suppress_discord_link_embed(url) for url in attempted_urls if url
    )
    if len(attempted_display) > 1000:
        attempted_display = f"{attempted_display[:997]}…"

    fallback_display = ", ".join(
        _suppress_discord_link_embed(url) for url in fallback_urls if url
    )
    if len(fallback_display) > 1000:
        fallback_display = f"{fallback_display[:997]}…"

    embed = discord.Embed(
        title="Media download failure",
        description=item.label or "Unknown attachment",
        color=discord.Color.red(),
        timestamp=utcnow(),
    )
    embed.add_field(name="HTTP status", value=f"{error.status}", inline=True)
    error_message = getattr(error, "message", None) or getattr(error, "history", None) or str(error)
    embed.add_field(name="Error detail", value=error_message[:1024] or "N/A", inline=False)
    request_info = getattr(error, "request_info", None)
    real_url = getattr(request_info, "real_url", None) if request_info else None
    real_url_str = str(real_url) if real_url else None
    request_method = getattr(request_info, "method", None) if request_info else None
    if request_method or real_url:
        request_value = " "
        if request_method:
            request_value = request_method
        if real_url_str:
            request_value = f"{request_value} {real_url_str}".strip()
        if len(request_value) > 1024:
            request_value = f"{request_value[:1021]}…"
        embed.add_field(name="Request", value=request_value, inline=False)

    if attempted_display:
        embed.add_field(name="Attempted URLs", value=attempted_display, inline=False)
    if fallback_display:
        embed.add_field(name="Fallback URLs", value=fallback_display, inline=False)

    if real_url_str and real_url_str not in attempted_urls:
        resolved_url = _suppress_discord_link_embed(real_url_str)
        if len(resolved_url) > 1024:
            resolved_url = f"{resolved_url[:1021]}…"
        embed.add_field(name="Resolved URL", value=resolved_url, inline=False)

    context_bits = {
        "guild": metadata.get("guild_id") or context.guild_id,
        "channel": metadata.get("channel_id"),
        "message": metadata.get("message_id"),
        "attachment": metadata.get("attachment_id"),
    }
    context_lines = [f"{key}: {value}" for key, value in context_bits.items() if value is not None]
    if context_lines:
        embed.add_field(name="Context", value="\n".join(context_lines), inline=False)

    if message is not None and getattr(message, "jump_url", None):
        embed.add_field(name="Source message", value=message.jump_url, inline=False)

    if item.source:
        embed.add_field(name="Source", value=item.source, inline=True)
    if item.ext_hint:
        embed.add_field(name="Extension", value=item.ext_hint, inline=True)

    attachment_size = metadata.get("size")
    if isinstance(attachment_size, int):
        embed.add_field(name="Size", value=f"{attachment_size} bytes", inline=True)

    interesting_headers: list[str] = []
    headers = getattr(error, "headers", None)
    if headers:
        for header_key in (
            "Content-Type",
            "Content-Length",
            "Cache-Control",
            "Age",
            "Via",
            "CF-Ray",
            "CF-Cache-Status",
            "Server",
        ):
            header_value = headers.get(header_key)
            if header_value:
                interesting_headers.append(f"{header_key}: {header_value}")
        if not interesting_headers:
            for header_key, header_value in list(headers.items())[:5]:
                interesting_headers.append(f"{header_key}: {header_value}")
    if interesting_headers:
        header_text = "\n".join(interesting_headers)
        if len(header_text) > 1024:
            header_text = f"{header_text[:1021]}…"
        embed.add_field(name="Response headers", value=header_text, inline=False)

    success = await send_log_message(
        bot,
        embed=embed,
        logger=log,
        context="media_download_failure",
    )
    if not success:  # pragma: no cover - best effort logging
        log.debug("Failed to send download failure embed to LOG_CHANNEL_ID=%s", LOG_CHANNEL_ID, exc_info=True)


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

    async def _emit_diagnostic(reason: str, *, status: str | None = None, extra: Optional[dict[str, Any]] = None) -> None:
        if not LOG_CHANNEL_ID:
            return
        bot = getattr(scanner, "bot", None)
        if bot is None:
            return
        throttle_key = f"{diagnostic_key_base}::{reason}"
        if not _should_emit_diagnostic(throttle_key):
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
            log.debug("Failed to send NSFW diagnostic to channel %s", LOG_CHANNEL_ID, exc_info=True)

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
        should_emit_diagnostic = True
        if reason == "unsupported_type" and item.tenor and item.source == "content":
            # Tenor share links that appear in message content typically provide an
            # embed with the actual media.  The content URL itself is just a HTML
            # landing page, which we intentionally skip.  Emitting a diagnostic for
            # these items is confusing because the embed will still be scanned
            # successfully.  Suppress the diagnostic for this specific case so the
            # log channel only reflects actionable skips.
            should_emit_diagnostic = False
        if should_emit_diagnostic:
            await _emit_diagnostic(reason, status=status or reason, extra=extra)
        return payload

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

    download: DownloadResult | None = None
    candidate_urls: list[str] = []
    seen_candidates: set[str] = set()

    def _add_candidate(url: str | None, *, update_metadata: bool = False) -> None:
        if not url:
            return
        normalised = _normalise_candidate_url(item, url)
        effective = normalised or (url.strip() if isinstance(url, str) else url)
        if not isinstance(effective, str) or not effective:
            return
        if update_metadata:
            if normalised and normalised not in fallback_urls_updated:
                fallback_urls_updated.append(normalised)
            elif effective not in fallback_urls_updated:
                fallback_urls_updated.append(effective)
        if effective in seen_candidates:
            return
        candidate_urls.append(effective)
        seen_candidates.add(effective)

    fallback_urls_raw = item.metadata.get("fallback_urls")
    if isinstance(fallback_urls_raw, list):
        fallback_urls_list = fallback_urls_raw
    elif isinstance(fallback_urls_raw, tuple):
        fallback_urls_list = list(fallback_urls_raw)
        item.metadata["fallback_urls"] = fallback_urls_list
    else:
        fallback_urls_list = []
        if isinstance(fallback_urls_raw, str) and fallback_urls_raw:
            fallback_urls_list.append(fallback_urls_raw)
        item.metadata["fallback_urls"] = fallback_urls_list
    fallback_urls_updated: list[str] = []

    _add_candidate(item.url)

    for candidate in fallback_urls_list:
        _add_candidate(candidate, update_metadata=True)

    if fallback_urls_updated and fallback_urls_updated != fallback_urls_list:
        fallback_urls_list[:] = fallback_urls_updated

    if not candidate_urls and isinstance(item.url, str) and item.url:
        candidate_urls.append(item.url)

    refreshed_attachment_attempted = False
    attempted_urls: list[str] = []

    try:
        async with AsyncExitStack() as stack:
            last_http_error: aiohttp.ClientResponseError | None = None
            for candidate_url in candidate_urls:
                attempted_urls.append(candidate_url)
                try:
                    download = await stack.enter_async_context(
                        temp_download(
                            scanner.session,
                            candidate_url,
                            guild_key=_guild_key(context),
                            limits=context.limits,
                            ext=item.ext_hint,
                            prefer_video=item.prefer_video,
                            head_cache=context.head_cache,
                        )
                    )
                    break
                except aiohttp.ClientResponseError as http_error:
                    is_last_candidate = candidate_url == candidate_urls[-1]
                    if http_error.status in {401, 403, 404}:
                        added_candidates = False
                        if not refreshed_attachment_attempted:
                            refreshed_attachment_attempted = True
                            refreshed_candidates = await _resolve_attachment_refresh_candidates(
                                scanner,
                                item=item,
                                message=message,
                            )
                            new_candidates = [
                                refreshed
                                for refreshed in refreshed_candidates
                                if refreshed and refreshed not in candidate_urls
                            ]
                            if new_candidates:
                                candidate_urls.extend(new_candidates)
                                for refreshed in new_candidates:
                                    if refreshed not in fallback_urls_list:
                                        fallback_urls_list.append(refreshed)
                                added_candidates = True
                                log.debug(
                                    "Refreshed attachment URL for %s via message fetch",
                                    item.label,
                                )
                        if added_candidates:
                            continue
                        if not is_last_candidate:
                            last_http_error = http_error
                            log.debug(
                                "Download failed for %s (HTTP %s); trying fallback",
                                candidate_url,
                                http_error.status,
                            )
                            continue
                    last_http_error = http_error
                    await _notify_download_failure(
                        scanner,
                        item=item,
                        context=context,
                        message=message,
                        attempted_urls=attempted_urls,
                        fallback_urls=fallback_urls_list,
                        error=http_error,
                    )
                    setattr(http_error, "_download_failure_logged", True)
                    raise
            if download is None:
                if last_http_error is not None:
                    await _notify_download_failure(
                        scanner,
                        item=item,
                        context=context,
                        message=message,
                        attempted_urls=attempted_urls or [item.url],
                        fallback_urls=fallback_urls_list,
                        error=last_http_error,
                    )
                    setattr(last_http_error, "_download_failure_logged", True)
                    raise last_http_error
                raise RuntimeError(f"Failed to resolve download URL for {item.label}")
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
        await _resolve_skip(
            "download_restricted",
            {"error": str(download_error)},
            status="download_restricted",
        )
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        log.debug("Skipping media %s due to download restriction: %s", item.url, download_error)
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
        if not getattr(http_error, "_download_failure_logged", False):
            await _notify_download_failure(
                scanner,
                item=item,
                context=context,
                message=message,
                attempted_urls=attempted_urls or [item.url],
                fallback_urls=fallback_urls_list,
                error=http_error,
            )
            setattr(http_error, "_download_failure_logged", True)
        await _resolve_skip(
            "http_error",
            {
                "error_status": http_error.status,
                "error_message": http_error.message,
                "attempted_urls": ", ".join(attempted_urls or []) or item.url,
                "fallback_urls": ", ".join(fallback_urls_list),
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
