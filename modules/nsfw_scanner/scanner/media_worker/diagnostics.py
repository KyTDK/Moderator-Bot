from __future__ import annotations

import logging
from typing import Any, Callable, Iterable, NamedTuple, Sequence
from urllib.parse import urlparse

import aiohttp
import discord
from discord.utils import utcnow

from modules.nsfw_scanner.constants import LOG_CHANNEL_ID
from modules.nsfw_scanner.context import GuildScanContext
from modules.nsfw_scanner.reporting import emit_verbose_report
from modules.nsfw_scanner.scanner.work_item import MediaWorkItem
from modules.nsfw_scanner.scanner.media_worker.cache import (
    annotate_cache_status,
    clone_scan_result,
)
from modules.utils.log_channel import send_log_message

from ...utils.diagnostics import (
    DiagnosticRateLimiter,
    extract_context_lines,
    truncate_field_value,
)

class AttemptedURL(NamedTuple):
    url: str
    source: str | None = None


_ATTEMPT_SOURCE_LABELS: dict[str, str] = {
    "work_item": "work item URL",
    "metadata_candidate": "metadata candidate URL",
    "metadata_fallback": "metadata fallback URL",
    "metadata_refreshed": "metadata refreshed URL",
}


def describe_attempt_source(source: str | None) -> str | None:
    if not source:
        return None
    return _ATTEMPT_SOURCE_LABELS.get(source, source)


log = logging.getLogger(__name__)

_DIAGNOSTIC_LIMITER = DiagnosticRateLimiter()


def should_emit_diagnostic(key: str) -> bool:
    return _DIAGNOSTIC_LIMITER.should_emit(key)


def suppress_discord_link_embed(url: str) -> str:
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


async def notify_download_failure(
    scanner,
    *,
    item: MediaWorkItem,
    context: GuildScanContext,
    message: discord.Message | None,
    attempted_urls: Iterable[AttemptedURL | Sequence[str] | str],
    fallback_urls: Iterable[str],
    refreshed_urls: Iterable[str] | None,
    error: aiohttp.ClientResponseError,
    logger: logging.Logger | None = None,
) -> None:
    if not LOG_CHANNEL_ID:
        return
    bot = getattr(scanner, "bot", None)
    if bot is None:
        return

    logger = logger or log

    metadata = item.metadata or {}

    def _coerce_attempted(entry: AttemptedURL | Sequence[str] | str) -> AttemptedURL:
        if isinstance(entry, AttemptedURL):
            return entry
        if isinstance(entry, Sequence) and not isinstance(entry, (str, bytes, bytearray)):
            if not entry:
                raise ValueError("empty attempted URL entry")
            url_value = str(entry[0]) if entry[0] else ""
            source_value = (
                str(entry[1]) if len(entry) > 1 and entry[1] is not None else None
            )
            return AttemptedURL(url=url_value, source=source_value)
        return AttemptedURL(url=str(entry), source=None)

    attempted_info: list[AttemptedURL] = []
    for raw_entry in attempted_urls:
        if not raw_entry:
            continue
        try:
            coerced = _coerce_attempted(raw_entry)
        except ValueError:
            continue
        if coerced.url:
            attempted_info.append(coerced)

    attempted_strings = [attempt.url for attempt in attempted_info]

    fallback_list = [url for url in fallback_urls if url]
    refreshed_list = [url for url in (refreshed_urls or []) if url]

    proxy_url = metadata.get("proxy_url") or (
        getattr(item.attachment, "proxy_url", None) if item.attachment else None
    )
    original_url = metadata.get("original_url") or (
        getattr(item.attachment, "url", None) if item.attachment else None
    )

    if proxy_url is not None and not isinstance(proxy_url, str):
        proxy_url = str(proxy_url)
    if original_url is not None and not isinstance(original_url, str):
        original_url = str(original_url)

    def _format_single(url_value: str) -> str:
        formatted = suppress_discord_link_embed(url_value)
        return truncate_field_value(formatted)

    def _format_join(
        url_values: Iterable[Any],
        *,
        separator: str,
        formatter: Callable[[Any], str] | None = None,
    ) -> str:
        apply_formatter = formatter or _format_single
        formatted_values = [apply_formatter(url) for url in url_values]
        joined = separator.join(formatted_values)
        if len(joined) > 1000:
            return f"{joined[:997]}…"
        return joined

    seen_primary = {value for value in (proxy_url, original_url) if value}
    additional_attempts = [
        attempt for attempt in attempted_info if attempt.url not in seen_primary
    ]
    fallback_filtered = [
        url for url in fallback_list if url not in seen_primary
    ]
    refreshed_filtered = [
        url for url in refreshed_list if url not in seen_primary
    ]

    def _format_attempt_entry(entry: AttemptedURL) -> str:
        base = suppress_discord_link_embed(entry.url)
        source_label = describe_attempt_source(entry.source)
        if source_label:
            value = f"{base} ({source_label})"
        else:
            value = base
        return truncate_field_value(value)

    attempted_display = _format_join(
        additional_attempts,
        separator="\n",
        formatter=_format_attempt_entry,
    )
    fallback_display = _format_join(fallback_filtered, separator=", ")
    refreshed_display = _format_join(refreshed_filtered, separator=", ")

    embed = discord.Embed(
        title="Media download failure",
        description=item.label or "Unknown attachment",
        color=discord.Color.red(),
        timestamp=utcnow(),
    )
    embed.add_field(name="HTTP status", value=f"{error.status}", inline=True)
    error_message = getattr(error, "message", None) or getattr(error, "history", None) or str(error)
    embed.add_field(
        name="Error detail",
        value=truncate_field_value(error_message or "N/A"),
        inline=False,
    )
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
        embed.add_field(
            name="Request",
            value=truncate_field_value(request_value),
            inline=False,
        )

    if proxy_url:
        embed.add_field(
            name="Proxy URL",
            value=_format_single(proxy_url),
            inline=False,
        )
    if original_url and (original_url != proxy_url or not proxy_url):
        embed.add_field(
            name="Original URL",
            value=_format_single(original_url),
            inline=False,
        )
    if attempted_display:
        embed.add_field(
            name="Additional Attempted URLs",
            value=attempted_display,
            inline=False,
        )
    if fallback_display:
        embed.add_field(name="Fallback URLs", value=fallback_display, inline=False)
    if refreshed_display:
        embed.add_field(name="Refreshed URLs", value=refreshed_display, inline=False)

    if real_url_str and real_url_str not in attempted_strings:
        resolved_url = suppress_discord_link_embed(real_url_str)
        embed.add_field(
            name="Resolved URL",
            value=truncate_field_value(resolved_url),
            inline=False,
        )

    context_lines = extract_context_lines(
        metadata=metadata,
        fallback_guild_id=context.guild_id,
        include_attachment=True,
    )
    if context_lines:
        embed.add_field(name="Context", value="\n".join(context_lines), inline=False)

    if message is not None and getattr(message, "jump_url", None):
        embed.add_field(
            name="Source message",
            value=truncate_field_value(message.jump_url),
            inline=False,
        )

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
        header_text = truncate_field_value("\n".join(interesting_headers))
        embed.add_field(name="Response headers", value=header_text, inline=False)

    success = await send_log_message(
        bot,
        embed=embed,
        logger=logger,
        context="media_download_failure",
    )
    if not success:  # pragma: no cover - best effort logging
        logger.debug(
            "Failed to send download failure embed to LOG_CHANNEL_ID=%s",
            LOG_CHANNEL_ID,
            exc_info=True,
        )


async def emit_verbose_if_needed(
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
    payload = annotate_cache_status(clone_scan_result(scan_result), cache_status)
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


__all__ = [
    "should_emit_diagnostic",
    "suppress_discord_link_embed",
    "AttemptedURL",
    "describe_attempt_source",
    "notify_download_failure",
    "emit_verbose_if_needed",
]
