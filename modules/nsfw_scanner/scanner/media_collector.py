from __future__ import annotations

import logging
import os
import re
from typing import Iterable, List
from urllib.parse import urlparse

import discord

from cogs.hydration import wait_for_hydration

from ..context import GuildScanContext
from ..constants import LOG_CHANNEL_ID
from .work_item import MediaWorkItem
from modules.utils.discord_utils import safe_get_channel

log = logging.getLogger(__name__)
_URL_RE = re.compile(r"https?://[^\s<>]+")


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
    attachments = getattr(message, "attachments", None) or []
    embeds = getattr(message, "embeds", None) or []
    stickers = getattr(message, "stickers", None) or []
    original_stats = _media_stats(message)
    if attachments or embeds or stickers:
        return message

    content = getattr(message, "content", "") or ""
    if "http" not in content:
        return message

    reasons: list[str] = []
    hydrated: discord.Message | None = None
    hydrated_stats: dict[str, int] | None = None
    fetched_stats: dict[str, int] | None = None
    try:
        hydrated = await wait_for_hydration(message)
    except Exception as exc:
        log.debug("hydrate_message: hydration wait failed for message %s: %s", getattr(message, "id", "?"), exc)
        reasons.append(f"Hydration wait failed: {exc}")

    if hydrated is not None:
        hydrated_stats = _media_stats(hydrated)
        if _has_media_metadata(hydrated_stats):
            return hydrated
        reasons.append(
            "Hydration waiter returned payload without media metadata "
            f"(counts: {_summarise_stats(hydrated_stats)})"
        )

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
            reasons.append(fetch_reason)

    if reasons and bot is not None and LOG_CHANNEL_ID:
        debug_context = {
            "content_urls": _extract_urls(content),
            "original_stats": _summarise_stats(original_stats),
            "hydrated_stats": _summarise_stats(hydrated_stats) if hydrated_stats else None,
            "fetched_stats": _summarise_stats(fetched_stats) if fetched_stats else None,
        }
        await _notify_hydration_issue(bot, message, reasons, debug_context)

    return hydrated or message


async def _notify_hydration_issue(
    bot: discord.Client,
    message: discord.Message,
    reasons: list[str],
    debug_context: dict[str, str | list[str] | None] | None = None,
) -> None:
    guild = getattr(message, "guild", None)
    if guild is None:
        return
    try:
        channel = await safe_get_channel(bot, LOG_CHANNEL_ID)
    except Exception as exc:
        log.warning("hydrate_message: failed to resolve LOG_CHANNEL_ID=%s: %s", LOG_CHANNEL_ID, exc)
        return
    if channel is None:
        log.warning("hydrate_message: LOG_CHANNEL_ID=%s not found", LOG_CHANNEL_ID)
        return

    jump_url = getattr(message, "jump_url", None)
    summary = f"Message `{getattr(message, 'id', 'unknown')}` in <#{getattr(getattr(message, 'channel', None), 'id', 0)}> could not be hydrated."
    embed = discord.Embed(title="Media Hydration Failed", description=summary, color=discord.Color.orange())
    reason_text = "\n".join(f"- {reason}" for reason in reasons)
    embed.add_field(name="Details", value=reason_text[:1024], inline=False)
    if jump_url:
        embed.add_field(name="Jump", value=f"[Open Message]({jump_url})", inline=False)
    if debug_context:
        media_stats_lines: list[str] = []
        original_stats = debug_context.get("original_stats")
        if original_stats:
            media_stats_lines.append(f"Original: {original_stats}")
        hydrated_stats = debug_context.get("hydrated_stats")
        if hydrated_stats:
            media_stats_lines.append(f"Hydrated: {hydrated_stats}")
        fetched_stats = debug_context.get("fetched_stats")
        if fetched_stats:
            media_stats_lines.append(f"Fetched: {fetched_stats}")
        if media_stats_lines:
            embed.add_field(name="Media Stats", value="\n".join(media_stats_lines)[:1024], inline=False)
        urls = debug_context.get("content_urls") or []
        if urls:
            embed.add_field(name="Detected URLs", value="\n".join(urls)[:1024], inline=False)
    content = None
    allowed_mentions = discord.AllowedMentions.none()
    try:
        await channel.send(content=content, embed=embed, allowed_mentions=allowed_mentions)
    except Exception as exc:
        log.warning("hydrate_message: failed to send hydration alert for message %s: %s", getattr(message, "id", "?"), exc)


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

    items: list[MediaWorkItem] = []
    for attachment in attachments:
        url = getattr(attachment, "proxy_url", None) or getattr(attachment, "url", None)
        if not url:
            continue
        filename = getattr(attachment, "filename", None) or url
        ext = os.path.splitext(filename)[1]
        cache_hint = getattr(attachment, "hash", None)
        metadata: dict[str, object] = {
            "size": getattr(attachment, "size", None),
            "attachment_id": getattr(attachment, "id", None),
        }
        if cache_hint:
            metadata["cache_key"] = f"hash::{cache_hint}"
        items.append(
            MediaWorkItem(
                source="attachment",
                label=filename,
                url=url,
                ext_hint=ext or None,
                metadata=metadata,
            )
        )

    seen_urls: set[str] = set()
    for embed in embeds:
        tenor_added = False
        for candidate in _extract_embed_urls(embed):
            if candidate in seen_urls:
                continue
            parsed = urlparse(candidate)
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
