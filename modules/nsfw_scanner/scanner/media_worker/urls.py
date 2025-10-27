from __future__ import annotations

from typing import Iterable
from urllib.parse import quote, urlparse, urlunparse

import discord

from ..work_item import MediaWorkItem
from modules.utils.discord_utils import safe_get_channel


async def resolve_attachment_refresh_candidates(
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
    for attachment in getattr(refreshed_message, "attachments", ()):  # pragma: no cover - discord models
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


def normalise_candidate_url(item: MediaWorkItem, candidate: str | None) -> str | None:
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


def extend_unique(target: list[str], candidates: Iterable[str]) -> None:
    seen = set(target)
    for candidate in candidates:
        if candidate and candidate not in seen:
            target.append(candidate)
            seen.add(candidate)


__all__ = [
    "resolve_attachment_refresh_candidates",
    "normalise_candidate_url",
    "extend_unique",
]
