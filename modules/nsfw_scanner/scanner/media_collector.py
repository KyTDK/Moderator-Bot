from __future__ import annotations

import logging
import os
import re
from typing import Iterable
from urllib.parse import urlparse, urlunparse

import discord

from cogs.hydration import wait_for_hydration

from ..context import GuildScanContext
from .work_item import MediaWorkItem
from ..utils.file_types import ANIMATED_EXTS, IMAGE_EXTS, VIDEO_EXTS

log = logging.getLogger(__name__)
_URL_RE = re.compile(r"https?://[^\s<>]+")
ALLOWED_CONTENT_EXTS = ANIMATED_EXTS | VIDEO_EXTS | IMAGE_EXTS


def _media_stats(message: discord.Message | None) -> dict[str, int]:
    stats = {
        "attachments": 0,
        "embeds": 0,
        "stickers": 0,
        "snapshots": 0,
        "snapshot_attachments": 0,
        "snapshot_embeds": 0,
        "snapshot_stickers": 0,
    }
    if message is None:
        return stats

    attachments = getattr(message, "attachments", None) or []
    embeds = getattr(message, "embeds", None) or []
    stickers = getattr(message, "stickers", None) or []

    stats["attachments"] = len(attachments)
    stats["embeds"] = len(embeds)
    stats["stickers"] = len(stickers)

    snapshots = list(getattr(message, "message_snapshots", None) or [])
    stats["snapshots"] = len(snapshots)
    for snapshot in snapshots:
        stats["snapshot_attachments"] += len(getattr(snapshot, "attachments", None) or [])
        stats["snapshot_embeds"] += len(getattr(snapshot, "embeds", None) or [])
        stats["snapshot_stickers"] += len(getattr(snapshot, "stickers", None) or [])

    return stats


def _has_media_metadata(stats: dict[str, int]) -> bool:
    return any(
        stats[key]
        for key in (
            "attachments",
            "embeds",
            "stickers",
            "snapshot_attachments",
            "snapshot_embeds",
            "snapshot_stickers",
        )
    )


def _summarise_stats(stats: dict[str, int]) -> str:
    return ", ".join(f"{key}={value}" for key, value in stats.items())


def _extract_urls(content: str | None, limit: int = 3) -> list[str]:
    if not content:
        return []
    matches = _URL_RE.findall(content)
    return matches[:limit]


def _derive_discord_cdn_fallback(url: str) -> str | None:
    """Return the original Discord CDN URL for a proxy attachment URL."""

    if not isinstance(url, str) or not url:
        return None

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return None

    netloc = parsed.netloc.lower()
    if not netloc.endswith("discordapp.net"):
        return None

    # Discord proxy URLs for attachments follow /attachments/<channel>/<id>/...
    if not parsed.path.startswith("/attachments/"):
        return None

    return urlunparse(("https", "cdn.discordapp.com", parsed.path, "", "", ""))


async def hydrate_message(message: discord.Message, bot: discord.Client | None = None) -> discord.Message:
    _ = bot  # Parameter kept for API compatibility; no runtime usage.
    attachments = getattr(message, "attachments", None) or []
    embeds = getattr(message, "embeds", None) or []
    stickers = getattr(message, "stickers", None) or []
    if attachments or embeds or stickers:
        return message

    content = getattr(message, "content", "") or ""
    if "http" not in content:
        return message

    hydrated: discord.Message | None = None
    hydrated_stats: dict[str, int] | None = None
    fetched_stats: dict[str, int] | None = None
    try:
        hydrated = await wait_for_hydration(message)
    except Exception as exc:
        log.debug("hydrate_message: hydration wait failed for message %s: %s", getattr(message, "id", "?"), exc)

    if hydrated is not None:
        hydrated_stats = _media_stats(hydrated)
        if _has_media_metadata(hydrated_stats):
            return hydrated

    channel = getattr(message, "channel", None)
    message_id = getattr(message, "id", None)
    if channel is not None and message_id is not None and hasattr(channel, "fetch_message"):
        fetch_reason: str | None = None
        try:
            fetched = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden):
            fetch_reason = "Message fetch returned NotFound/Forbidden"
        except discord.HTTPException as exc:
            log.debug("hydrate_message: fetch fallback failed for message %s: %s", message_id, exc)
            fetch_reason = f"Message fetch raised HTTPException: {exc}"
        else:
            fetched_stats = _media_stats(fetched)
            if _has_media_metadata(fetched_stats):
                return fetched
            fetch_reason = (
                "Fetched message still missing media metadata "
                f"(counts: {_summarise_stats(fetched_stats)})"
            )
        if fetch_reason:
            log.debug("hydrate_message: %s", fetch_reason)

    return hydrated or message
def collect_media_items(
    message: discord.Message,
    bot: discord.Client,
    context: GuildScanContext,
) -> list[MediaWorkItem]:
    snapshots = getattr(message, "message_snapshots", None) or []
    snapshot = snapshots[0] if snapshots else None

    attachments = list(getattr(message, "attachments", None) or []) or list(
        getattr(snapshot, "attachments", None) or []
    )
    embeds = list(getattr(message, "embeds", None) or []) or list(
        getattr(snapshot, "embeds", None) or []
    )
    stickers = list(getattr(message, "stickers", None) or []) or list(
        getattr(snapshot, "stickers", None) or []
    )

    message_id = getattr(message, "id", None)
    message_channel = getattr(message, "channel", None)
    message_channel_id = getattr(message_channel, "id", None)
    message_guild = getattr(message, "guild", None)
    message_guild_id = getattr(message_guild, "id", None)

    snapshot_id = getattr(snapshot, "id", None) if snapshot else None
    snapshot_channel_id = getattr(snapshot, "channel_id", None) if snapshot else None
    snapshot_guild_id = getattr(snapshot, "guild_id", None) if snapshot else None

    items: list[MediaWorkItem] = []
    seen_urls: set[str] = set()
    attachments_by_filename: dict[str, discord.Attachment] = {}
    for attachment in attachments:
        raw_candidates: list[str] = []
        proxy_url = getattr(attachment, "proxy_url", None)
        if proxy_url:
            raw_candidates.append(proxy_url)
        raw_url = getattr(attachment, "url", None)
        if raw_url and raw_url not in raw_candidates:
            raw_candidates.append(raw_url)

        filename = getattr(attachment, "filename", None) or (raw_candidates[0] if raw_candidates else None)
        ext = os.path.splitext(filename)[1] if filename else ""

        url_candidates: list[str] = []
        for candidate in raw_candidates:
            if not candidate or candidate in url_candidates:
                continue
            parsed_candidate = urlparse(candidate)
            if parsed_candidate.scheme not in {"http", "https"}:
                continue
            url_candidates.append(candidate)

        if url_candidates and len(url_candidates) == 1:
            derived_fallback = _derive_discord_cdn_fallback(url_candidates[0])
            if derived_fallback and derived_fallback not in url_candidates:
                url_candidates.append(derived_fallback)

        if not url_candidates:
            continue
        primary_url = url_candidates[0]
        fallback_urls = url_candidates[1:]
        label_value = filename or primary_url
        cache_hint = getattr(attachment, "hash", None)
        metadata: dict[str, object] = {
            "size": getattr(attachment, "size", None),
            "attachment_id": getattr(attachment, "id", None),
            "message_id": message_id or snapshot_id,
            "channel_id": message_channel_id or snapshot_channel_id,
            "guild_id": message_guild_id or snapshot_guild_id,
        }
        if cache_hint:
            metadata["cache_key"] = f"hash::{cache_hint}"
        if fallback_urls:
            metadata["fallback_urls"] = fallback_urls
        for candidate in url_candidates:
            seen_urls.add(candidate)
        if filename:
            attachments_by_filename.setdefault(filename, attachment)
        items.append(
            MediaWorkItem(
                source="attachment",
                label=label_value,
                url=primary_url,
                ext_hint=ext or None,
                metadata=metadata,
            )
        )

    for embed in embeds:
        tenor_added = False
        for candidate in _extract_embed_urls(embed):
            resolved_candidate = _resolve_embed_url(candidate, attachments_by_filename)
            if not resolved_candidate:
                continue
            parsed_candidate = urlparse(resolved_candidate)
            if parsed_candidate.scheme not in {"http", "https"}:
                continue
            candidate = resolved_candidate
            if candidate in seen_urls:
                continue
            parsed = parsed_candidate
            domain = parsed.netloc.lower()
            is_tenor = domain == "tenor.com" or domain.endswith(".tenor.com")
            if is_tenor and tenor_added:
                continue
            if is_tenor and not context.tenor_allowed:
                continue
            seen_urls.add(candidate)
            if is_tenor:
                tenor_added = True
            ext = os.path.splitext(parsed.path)[1]
            items.append(
                MediaWorkItem(
                    source="embed",
                    label=candidate,
                    url=candidate,
                    prefer_video=is_tenor,
                    ext_hint=ext or None,
                    tenor=is_tenor,
                )
        )

    for sticker in stickers:
        sticker_url = getattr(sticker, "url", None)
        if not sticker_url:
            continue
        fmt = getattr(getattr(sticker, "format", None), "name", None)
        label = getattr(sticker, "name", None) or sticker_url
        ext = f".{fmt.lower()}" if fmt else None
        items.append(
            MediaWorkItem(
                source="sticker",
                label=label,
                url=sticker_url,
                ext_hint=ext,
                metadata={"sticker_format": (fmt or "").lower()},
            )
        )

    for emoji in _extract_custom_emojis(message, bot):
        emoji_url = str(getattr(emoji, "url", None) or "")
        if not emoji_url:
            continue
        label = getattr(emoji, "name", None) or emoji_url
        animated = getattr(emoji, "animated", False)
        ext = ".gif" if animated else ".png"
        items.append(
            MediaWorkItem(
                source="emoji",
                label=label,
                url=emoji_url,
                prefer_video=animated,
                ext_hint=ext,
                metadata={"emoji_id": getattr(emoji, "id", None)},
            )
        )

    for source in (message, snapshot):
        content = getattr(source, "content", None) if source is not None else None
        if not content:
            continue
        for candidate in _extract_urls(content):
            if candidate in seen_urls:
                continue
            parsed = urlparse(candidate)
            domain = parsed.netloc.lower()
            tenor = domain == "tenor.com" or domain.endswith(".tenor.com")
            if tenor and not context.tenor_allowed:
                continue
            ext = os.path.splitext(parsed.path)[1].lower()
            if not tenor and ext not in ALLOWED_CONTENT_EXTS:
                continue
            prefer_video = tenor or ext in ANIMATED_EXTS or ext in VIDEO_EXTS
            seen_urls.add(candidate)
            items.append(
                MediaWorkItem(
                    source="content",
                    label=candidate,
                    url=candidate,
                    prefer_video=prefer_video,
                    ext_hint=ext or None,
                    tenor=tenor,
                )
            )

    return items


def _extract_embed_urls(embed: discord.Embed) -> list[str]:
    urls: list[str] = []
    video = getattr(embed, "video", None)
    if video and getattr(video, "url", None):
        urls.append(video.url)
    image = getattr(embed, "image", None)
    if image and getattr(image, "url", None):
        urls.append(image.url)
    thumbnail = getattr(embed, "thumbnail", None)
    if thumbnail and getattr(thumbnail, "url", None):
        urls.append(thumbnail.url)
    return urls


def _resolve_embed_url(
    candidate: str,
    attachments_by_filename: dict[str, discord.Attachment],
) -> str | None:
    parsed = urlparse(candidate)
    if parsed.scheme != "attachment":
        return candidate

    filename = parsed.path.lstrip("/")
    if not filename:
        return None

    attachment = attachments_by_filename.get(filename)
    if attachment is None:
        return None

    for attr in ("url", "proxy_url"):
        resolved = getattr(attachment, attr, None)
        if resolved:
            return resolved

    return None


def _extract_custom_emojis(
    message: discord.Message,
    bot: discord.Client,
) -> Iterable[discord.Emoji]:
    content = getattr(message, "content", "") or ""
    matches = set(re.findall(r"<a?:\w+:(\d+)>", content))
    for match in matches:
        try:
            emoji_id = int(match)
        except ValueError:
            continue
        emoji = bot.get_emoji(emoji_id)
        if emoji:
            yield emoji


__all__ = ["hydrate_message", "collect_media_items"]
