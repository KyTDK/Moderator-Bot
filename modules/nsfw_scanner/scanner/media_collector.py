from __future__ import annotations

import logging
import os
import re
from typing import Any, Iterable
from urllib.parse import urlparse

import discord

from cogs.hydration import wait_for_hydration
from weakref import WeakKeyDictionary

from ..context import GuildScanContext
from .work_item import MediaWorkItem
from ..utils.file_types import ANIMATED_EXTS, IMAGE_EXTS, VIDEO_EXTS

log = logging.getLogger(__name__)
_URL_RE = re.compile(r"https?://[^\s<>]+")
ALLOWED_CONTENT_EXTS = ANIMATED_EXTS | VIDEO_EXTS | IMAGE_EXTS


_HYDRATION_METADATA: "WeakKeyDictionary[discord.Message, dict[str, Any]]" = WeakKeyDictionary()


def _snapshot_attachment_state(message: discord.Message | None) -> dict[str, Any]:
    snapshot = {
        "proxy_list": [],
        "all_urls": [],
        "proxy_by_id": {},
        "proxy_by_index": {},
    }
    if message is None:
        return snapshot

    attachments = getattr(message, "attachments", None) or []
    for index, attachment in enumerate(attachments):
        proxy_url = getattr(attachment, "proxy_url", None)
        url = getattr(attachment, "url", None)

        proxy_str: str | None = None
        if proxy_url:
            proxy_str = str(proxy_url)
            snapshot["proxy_list"].append(proxy_str)
            if proxy_str not in snapshot["all_urls"]:
                snapshot["all_urls"].append(proxy_str)

        if url:
            url_str = str(url)
            if url_str not in snapshot["all_urls"]:
                snapshot["all_urls"].append(url_str)
            if proxy_str is None:
                proxy_str = url_str

        if proxy_str is None:
            continue

        attachment_id = getattr(attachment, "id", None)
        if attachment_id is not None:
            snapshot["proxy_by_id"][attachment_id] = proxy_str
        snapshot["proxy_by_index"][index] = proxy_str

    return snapshot


def _store_hydration_metadata(
    message: discord.Message | None,
    *,
    stage: str,
    original_snapshot: dict[str, Any],
    final_snapshot: dict[str, Any],
) -> None:
    if message is None:
        return
    try:
        _HYDRATION_METADATA[message] = {
            "stage": stage,
            "hydrated_urls": list(final_snapshot.get("all_urls", [])),
            "hydrated_proxy_urls": list(final_snapshot.get("proxy_list", [])),
            "original_proxy_urls": list(original_snapshot.get("proxy_list", [])),
            "original_proxy_map": dict(original_snapshot.get("proxy_by_id", {})),
            "original_proxy_index_map": dict(
                original_snapshot.get("proxy_by_index", {})
            ),
        }
    except TypeError:  # pragma: no cover - weakref unsupported
        return


def _get_hydration_metadata(message: discord.Message | None) -> dict[str, Any]:
    if message is None:
        return {}
    try:
        return _HYDRATION_METADATA.get(message, {})
    except TypeError:  # pragma: no cover - weakref unsupported
        return {}


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


async def hydrate_message(message: discord.Message, bot: discord.Client | None = None) -> discord.Message:
    _ = bot  # Parameter kept for API compatibility; no runtime usage.
    attachments = getattr(message, "attachments", None) or []
    original_snapshot = _snapshot_attachment_state(message)
    if attachments and all(
        (getattr(a, "proxy_url", None) or getattr(a, "url", None) or "").startswith(("http://", "https://"))
        for a in attachments
    ):
        _store_hydration_metadata(
            message,
            stage="skipped",
            original_snapshot=original_snapshot,
            final_snapshot=_snapshot_attachment_state(message),
        )
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
            _store_hydration_metadata(
                hydrated,
                stage="wait_for_hydration",
                original_snapshot=original_snapshot,
                final_snapshot=_snapshot_attachment_state(hydrated),
            )
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
                _store_hydration_metadata(
                    fetched,
                    stage="fetch_message",
                    original_snapshot=original_snapshot,
                    final_snapshot=_snapshot_attachment_state(fetched),
                )
                return fetched
            fetch_reason = (
                "Fetched message still missing media metadata "
                f"(counts: {_summarise_stats(fetched_stats)})"
            )
        if fetch_reason:
            log.debug("hydrate_message: %s", fetch_reason)

    final_message = hydrated or message
    final_stage = "wait_for_hydration" if hydrated is not None else "skipped"
    _store_hydration_metadata(
        final_message,
        stage=final_stage,
        original_snapshot=original_snapshot,
        final_snapshot=_snapshot_attachment_state(final_message),
    )
    return final_message
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

    hydration_info = _get_hydration_metadata(message)
    base_metadata: dict[str, Any] = {}
    hydration_stage = hydration_info.get("stage")
    if hydration_stage:
        base_metadata["hydration_stage"] = hydration_stage
    hydrated_urls = hydration_info.get("hydrated_urls")
    if hydrated_urls:
        base_metadata["hydrated_urls"] = list(hydrated_urls)
    original_proxy_map = hydration_info.get("original_proxy_map") or {}
    original_proxy_index_map = hydration_info.get("original_proxy_index_map") or {}

    items: list[MediaWorkItem] = []
    seen_urls: set[str] = set()
    attachments_by_filename: dict[str, discord.Attachment] = {}
    for attachment_index, attachment in enumerate(attachments):
        filename = getattr(attachment, "filename", None)
        proxy_url = getattr(attachment, "proxy_url", None)
        raw_url = getattr(attachment, "url", None)

        if proxy_url is not None and not isinstance(proxy_url, str):
            proxy_url = str(proxy_url)
        if raw_url is not None and not isinstance(raw_url, str):
            raw_url = str(raw_url)

        primary_url = proxy_url or raw_url or (filename or "")
        # Skip if no real CDN URL
        if not primary_url.startswith(("http://", "https://")):
            log.debug("Skipping attachment with no valid URL: %s", filename)
            continue
        if not isinstance(primary_url, str):
            primary_url = str(primary_url)
        if not primary_url:
            primary_url = filename or "attachment"
        if primary_url in seen_urls:
            continue

        ext = os.path.splitext(filename or "")[1]
        cache_hint = getattr(attachment, "hash", None)
        metadata: dict[str, object] = dict(base_metadata)
        metadata.update(
            {
            "size": getattr(attachment, "size", None),
            "attachment_id": getattr(attachment, "id", None),
            "message_id": message_id or snapshot_id,
            "channel_id": message_channel_id or snapshot_channel_id,
            "guild_id": message_guild_id or snapshot_guild_id,
        }
        )
        if proxy_url:
            metadata["proxy_url"] = proxy_url
        if raw_url:
            metadata["original_url"] = raw_url
        if proxy_url and raw_url and proxy_url != raw_url:
            metadata["fallback_urls"] = [raw_url]
        if cache_hint:
            metadata["cache_key"] = f"hash::{cache_hint}"

        original_proxy_url = None
        attachment_id = getattr(attachment, "id", None)
        if attachment_id is not None and attachment_id in original_proxy_map:
            original_proxy_url = original_proxy_map[attachment_id]
        elif attachment_index in original_proxy_index_map:
            original_proxy_url = original_proxy_index_map[attachment_index]
        if original_proxy_url:
            metadata["original_proxy_url"] = original_proxy_url

        seen_urls.add(primary_url)
        if filename:
            attachments_by_filename.setdefault(filename, attachment)
        items.append(
            MediaWorkItem(
                source="attachment",
                label=filename or primary_url,
                url=primary_url,
                ext_hint=ext or None,
                metadata=metadata,
                attachment=attachment,
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
            embed_metadata: dict[str, Any] = dict(base_metadata)
            items.append(
                MediaWorkItem(
                    source="embed",
                    label=candidate,
                    url=candidate,
                    prefer_video=is_tenor,
                    ext_hint=ext or None,
                    tenor=is_tenor,
                    metadata=embed_metadata,
                )
            )

    for sticker in stickers:
        sticker_url = getattr(sticker, "url", None)
        if not sticker_url:
            continue
        fmt = getattr(getattr(sticker, "format", None), "name", None)
        label = getattr(sticker, "name", None) or sticker_url
        ext = f".{fmt.lower()}" if fmt else None
        sticker_metadata: dict[str, Any] = dict(base_metadata)
        sticker_metadata["sticker_format"] = (fmt or "").lower()
        items.append(
            MediaWorkItem(
                source="sticker",
                label=label,
                url=sticker_url,
                ext_hint=ext,
                metadata=sticker_metadata,
            )
        )

    for emoji in _extract_custom_emojis(message, bot):
        emoji_url = str(getattr(emoji, "url", None) or "")
        if not emoji_url:
            continue
        label = getattr(emoji, "name", None) or emoji_url
        animated = getattr(emoji, "animated", False)
        ext = ".gif" if animated else ".png"
        emoji_metadata: dict[str, Any] = dict(base_metadata)
        emoji_metadata["emoji_id"] = getattr(emoji, "id", None)
        items.append(
            MediaWorkItem(
                source="emoji",
                label=label,
                url=emoji_url,
                prefer_video=animated,
                ext_hint=ext,
                metadata=emoji_metadata,
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
            content_metadata: dict[str, Any] = dict(base_metadata)
            items.append(
                MediaWorkItem(
                    source="content",
                    label=candidate,
                    url=candidate,
                    prefer_video=prefer_video,
                    ext_hint=ext or None,
                    tenor=tenor,
                    metadata=content_metadata,
                )
            )

    return items


def _extract_embed_urls(embed: discord.Embed) -> list[str]:
    urls: list[str] = []
    video = getattr(embed, "video", None)
    if video:
        proxy_url = getattr(video, "proxy_url", None)
        url = getattr(video, "url", None)
        if proxy_url or url:
            urls.append(proxy_url or url)
    image = getattr(embed, "image", None)
    if image:
        proxy_url = getattr(image, "proxy_url", None)
        url = getattr(image, "url", None)
        if proxy_url or url:
            urls.append(proxy_url or url)
    thumbnail = getattr(embed, "thumbnail", None)
    if thumbnail:
        proxy_url = getattr(thumbnail, "proxy_url", None)
        url = getattr(thumbnail, "url", None)
        if proxy_url or url:
            urls.append(proxy_url or url)
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

    for attr in ("proxy_url", "url"):
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
