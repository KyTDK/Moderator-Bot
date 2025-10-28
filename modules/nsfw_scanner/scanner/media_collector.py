from __future__ import annotations

import logging
import os
import re
from typing import Any
from urllib.parse import urlparse

import discord

from ..context import GuildScanContext
from ..utils.file_types import ANIMATED_EXTS, IMAGE_EXTS, VIDEO_EXTS
from .work_item import MediaWorkItem

log = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s<>]+")
ALLOWED_CONTENT_EXTS = ANIMATED_EXTS | VIDEO_EXTS | IMAGE_EXTS


def _is_http(u: str | None) -> bool:
    return bool(u) and isinstance(u, str) and u.startswith(("http://", "https://"))


def _extract_urls(content: str | None, limit: int = 3) -> list[str]:
    if not content:
        return []
    return _URL_RE.findall(content)[:limit]


def collect_media_items(
    message: discord.Message,
    bot: discord.Client,
    context: GuildScanContext,
) -> list[MediaWorkItem]:
    snapshots = getattr(message, "message_snapshots", None) or []
    snapshot = snapshots[0] if snapshots else None

    attachments = list(getattr(message, "attachments", None) or [])
    if not attachments and snapshot is not None:
        attachments = list(getattr(snapshot, "attachments", None) or [])

    embeds = list(getattr(message, "embeds", None) or [])
    if not embeds and snapshot is not None:
        embeds = list(getattr(snapshot, "embeds", None) or [])

    stickers = list(getattr(message, "stickers", None) or [])
    if not stickers and snapshot is not None:
        stickers = list(getattr(snapshot, "stickers", None) or [])

    message_id = getattr(message, "id", None)
    message_channel = getattr(message, "channel", None)
    message_channel_id = getattr(message_channel, "id", None)
    message_guild = getattr(message, "guild", None)
    message_guild_id = getattr(message_guild, "id", None)

    snapshot_id = getattr(snapshot, "id", None) if snapshot else None
    snapshot_channel_id = getattr(snapshot, "channel_id", None) if snapshot else None
    snapshot_guild_id = getattr(snapshot, "guild_id", None) if snapshot else None

    base_meta: dict[str, Any] = {}
    if message_id or snapshot_id:
        base_meta["message_id"] = message_id or snapshot_id
    if message_channel_id or snapshot_channel_id:
        base_meta["channel_id"] = message_channel_id or snapshot_channel_id
    if message_guild_id or snapshot_guild_id:
        base_meta["guild_id"] = message_guild_id or snapshot_guild_id

    items: list[MediaWorkItem] = []
    seen: set[str] = set()
    attachments_by_filename: dict[str, discord.Attachment] = {}

    for index, attachment in enumerate(attachments):
        filename = getattr(attachment, "filename", None)
        label = filename or getattr(attachment, "url", None) or getattr(attachment, "proxy_url", None)
        if not label:
            label = f"attachment-{index}"
        primary_url = getattr(attachment, "proxy_url", None) or getattr(attachment, "url", None)
        if filename:
            attachments_by_filename.setdefault(filename, attachment)
        if _is_http(primary_url):
            seen.add(str(primary_url))
        ext = os.path.splitext(filename or "")[1]
        meta = {
            **base_meta,
            "size": getattr(attachment, "size", None),
            "attachment_id": getattr(attachment, "id", None),
        }
        if primary_url:
            meta["download_url"] = str(primary_url)
        cache_hint = getattr(attachment, "hash", None)
        if cache_hint:
            meta["cache_key"] = f"hash::{cache_hint}"

        log.debug("collect: ATTACHMENT queued %s", label)
        items.append(
            MediaWorkItem(
                source="attachment",
                label=str(label),
                url=str(primary_url) if primary_url else str(label),
                ext_hint=ext or None,
                metadata=meta,
                attachment=attachment,
            )
        )

    for embed in embeds:
        tenor_added = False
        for candidate in _extract_embed_urls(embed):
            resolved = _resolve_embed_url(candidate, attachments_by_filename) or ""
            if not _is_http(resolved):
                continue
            if resolved in seen:
                continue
            parsed = urlparse(resolved)
            domain = parsed.netloc.lower()
            is_tenor = domain == "tenor.com" or domain.endswith(".tenor.com")
            if is_tenor:
                if tenor_added or not context.tenor_allowed:
                    continue
                tenor_added = True
            seen.add(resolved)
            ext = os.path.splitext(parsed.path)[1]
            items.append(
                MediaWorkItem(
                    source="embed",
                    label=resolved,
                    url=resolved,
                    prefer_video=is_tenor,
                    ext_hint=ext or None,
                    tenor=is_tenor,
                    metadata=dict(base_meta),
                )
            )

    for sticker in stickers:
        sticker_url = getattr(sticker, "url", None)
        if not _is_http(sticker_url):
            continue
        fmt = getattr(getattr(sticker, "format", None), "name", None)
        label = getattr(sticker, "name", None) or sticker_url
        ext = f".{fmt.lower()}" if fmt else None
        items.append(
            MediaWorkItem(
                source="sticker",
                label=label,
                url=str(sticker_url),
                ext_hint=ext,
                metadata={**base_meta, "sticker_format": (fmt or "").lower()},
            )
        )

    for src in (message, snapshot):
        text = getattr(src, "content", None) if src is not None else None
        if not text:
            continue
        for url in _extract_urls(text, limit=20):
            if url in seen:
                continue
            parsed = urlparse(url)
            domain = parsed.netloc.lower()
            tenor = domain == "tenor.com" or domain.endswith(".tenor.com")
            if tenor and not context.tenor_allowed:
                continue
            ext = os.path.splitext(parsed.path)[1].lower()
            if not tenor and ext not in ALLOWED_CONTENT_EXTS:
                continue
            seen.add(url)
            items.append(
                MediaWorkItem(
                    source="content",
                    label=url,
                    url=url,
                    prefer_video=tenor or ext in ANIMATED_EXTS or ext in VIDEO_EXTS,
                    ext_hint=ext or None,
                    tenor=tenor,
                    metadata=dict(base_meta),
                )
            )

    return items


def _extract_embed_urls(embed: discord.Embed) -> list[str]:
    urls: list[str] = []

    def add(proxy_url: str | None, url: str | None) -> None:
        if proxy_url:
            urls.append(str(proxy_url))
            return
        if url:
            urls.append(str(url))

    video = getattr(embed, "video", None)
    if video:
        add(getattr(video, "proxy_url", None), getattr(video, "url", None))
    image = getattr(embed, "image", None)
    if image:
        add(getattr(image, "proxy_url", None), getattr(image, "url", None))
    thumbnail = getattr(embed, "thumbnail", None)
    if thumbnail:
        add(getattr(thumbnail, "proxy_url", None), getattr(thumbnail, "url", None))

    return urls


def _resolve_embed_url(candidate: str, attachments_by_filename: dict[str, discord.Attachment]) -> str | None:
    parsed = urlparse(candidate)
    if parsed.scheme != "attachment":
        return candidate
    filename = parsed.path.lstrip("/")
    if not filename:
        return None
    attachment = attachments_by_filename.get(filename)
    if attachment is None:
        return None
    for attr in ("proxy_url", "url"):
        resolved = getattr(attachment, attr, None)
        if resolved:
            return str(resolved)
    return None


__all__ = ["collect_media_items"]