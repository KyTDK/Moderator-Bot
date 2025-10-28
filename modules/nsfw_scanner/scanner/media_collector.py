from __future__ import annotations

import logging
import os
import re
from typing import Any, Iterable, Tuple, List, Dict
from urllib.parse import urlparse, parse_qs

import discord
from weakref import WeakKeyDictionary

from cogs.hydration import wait_for_hydration

from ..context import GuildScanContext
from .work_item import MediaWorkItem
from ..utils.file_types import ANIMATED_EXTS, IMAGE_EXTS, VIDEO_EXTS

log = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s<>]+")
ALLOWED_CONTENT_EXTS = ANIMATED_EXTS | VIDEO_EXTS | IMAGE_EXTS
_SIGNED_DISCORD_DOMAINS = {"cdn.discordapp.com", "media.discordapp.net", "cdn.discordapp.net"}

_HYDRATION_METADATA: "WeakKeyDictionary[discord.Message, dict[str, Any]]" = WeakKeyDictionary()

def _is_http(u: str | None) -> bool:
    return bool(u) and isinstance(u, str) and u.startswith(("http://", "https://"))

def _host(u: str) -> str:
    try:
        return urlparse(u).netloc.lower()
    except Exception:
        return ""

def _is_discord_host(u: str) -> bool:
    return _host(u) in _SIGNED_DISCORD_DOMAINS

def _is_signed_discord_media_url(u: str | None) -> bool:
    if not u:
        return False
    u = str(u).strip().rstrip("&")
    return "?ex=" in u and "?is=" in u and "?hm=" in u

def _extract_urls(content: str | None, limit: int = 3) -> list[str]:
    if not content:
        return []
    return _URL_RE.findall(content)[:limit]

def _extract_signed_discord_urls(content: str | None) -> list[str]:
    return [u for u in _extract_urls(content, limit=20) if _is_signed_discord_media_url(u)]

def _map_signed_urls_to_filenames(urls: Iterable[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for u in urls:
        try:
            fn = os.path.basename(urlparse(u).path)
        except Exception:
            continue
        if fn and fn not in mapping:
            mapping[fn] = u
    return mapping

def _snapshot_attachment_state(message: discord.Message | None) -> dict[str, Any]:
    snap = {"proxy_list": [], "all_urls": [], "proxy_by_id": {}, "proxy_by_index": {}}
    if not message:
        return snap
    for idx, a in enumerate(getattr(message, "attachments", None) or []):
        p = getattr(a, "proxy_url", None)
        r = getattr(a, "url", None)
        p_s, r_s = (str(p) if p else None), (str(r) if r else None)
        if p_s:
            snap["proxy_list"].append(p_s)
            if p_s not in snap["all_urls"]:
                snap["all_urls"].append(p_s)
        if r_s:
            if r_s not in snap["all_urls"]:
                snap["all_urls"].append(r_s)
            if not p_s:
                p_s = r_s
        if not p_s:
            continue
        a_id = getattr(a, "id", None)
        if a_id is not None:
            snap["proxy_by_id"][a_id] = p_s
        snap["proxy_by_index"][idx] = p_s
    return snap

def _store_hydration_metadata(
    message: discord.Message | None,
    *,
    stage: str,
    original: dict[str, Any],
    final: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> None:
    if not message:
        return
    data: dict[str, Any] = {
        "stage": stage,
        "hydrated_urls": list(final.get("all_urls", [])),
        "hydrated_proxy_urls": list(final.get("proxy_list", [])),
        "original_proxy_urls": list(original.get("proxy_list", [])),
        "original_proxy_map": dict(original.get("proxy_by_id", {})),
        "original_proxy_index_map": dict(original.get("proxy_by_index", {})),
    }
    if extra:
        data.update(extra)
    try:
        _HYDRATION_METADATA[message] = data
    except TypeError:
        pass  # weakref unsupported in some edge mocks

def _get_hydration_metadata(message: discord.Message | None) -> dict[str, Any]:
    if not message:
        return {}
    try:
        return _HYDRATION_METADATA.get(message, {})
    except TypeError:
        return {}

def _media_counts(message: discord.Message | None) -> dict[str, int]:
    stats = dict(attachments=0, embeds=0, stickers=0, snapshots=0,
                 snapshot_attachments=0, snapshot_embeds=0, snapshot_stickers=0)
    if not message:
        return stats
    attachments = getattr(message, "attachments", None) or []
    embeds = getattr(message, "embeds", None) or []
    stickers = getattr(message, "stickers", None) or []
    stats["attachments"], stats["embeds"], stats["stickers"] = len(attachments), len(embeds), len(stickers)
    snaps = list(getattr(message, "message_snapshots", None) or [])
    stats["snapshots"] = len(snaps)
    for s in snaps:
        stats["snapshot_attachments"] += len(getattr(s, "attachments", None) or [])
        stats["snapshot_embeds"] += len(getattr(s, "embeds", None) or [])
        stats["snapshot_stickers"] += len(getattr(s, "stickers", None) or [])
    return stats

def _has_any_media(stats: dict[str, int]) -> bool:
    return any(stats[k] for k in ("attachments","embeds","stickers","snapshot_attachments","snapshot_embeds","snapshot_stickers"))

def _choose_best_attachment_url(
    filename: str | None,
    proxy_url: str | None,
    raw_url: str | None,
    signed_content_urls: List[str],
    filename_signed_map: Dict[str, str],
) -> Tuple[str | None, List[str]]:
    p = str(proxy_url) if proxy_url else None
    r = str(raw_url) if raw_url else None
    fallback: List[str] = []

    if _is_signed_discord_media_url(p):
        if r and r != p:
            fallback.append(r)
        return p, fallback

    if _is_signed_discord_media_url(r):
        if p and p != r:
            fallback.append(p)
        return r, fallback

    if _is_http(p) and not _is_discord_host(p):
        if r and r != p:
            fallback.append(r)
        return p, fallback
    if _is_http(r) and not _is_discord_host(r):
        if p and p != r:
            fallback.append(p)
        return r, fallback

    signed = filename_signed_map.get(filename or "")
    if not signed and signed_content_urls:
        signed = signed_content_urls[0]
    if signed:
        if p and p != signed:
            fallback.append(p)
        if r and r not in (signed, p):
            fallback.append(r)
        return signed, fallback

    if _is_http(p):
        if r and r != p:
            fallback.append(r)
        return p, fallback
    if _is_http(r):
        if p and p != r:
            fallback.append(p)
        return r, fallback

    return None, fallback

async def hydrate_message(message: discord.Message, bot: discord.Client | None = None) -> discord.Message:
    # Only skip if every attachment already has a usable URL: http(s) and (non-Discord OR signed).
    attachments = getattr(message, "attachments", None) or []
    content = getattr(message, "content", "") or ""
    original = _snapshot_attachment_state(message)

    def _usable(u: str) -> bool:
        return _is_http(u) and (not _is_discord_host(u) or _is_signed_discord_media_url(u))

    if attachments:
        raw_urls = [(str(getattr(a, "proxy_url", "") or getattr(a, "url", "") or "")) for a in attachments]
        if all(u and _usable(u) for u in raw_urls):
            _store_hydration_metadata(message, stage="skipped_usable", original=original, final=_snapshot_attachment_state(message))
            return message

    # Try Discord's internal hydration first (wait_for_hydration)
    hydrated: discord.Message | None = None
    try:
        hydrated = await wait_for_hydration(message)
    except Exception as exc:
        log.debug("hydrate_message: wait_for_hydration failed for %s: %r", getattr(message, "id", "?"), exc)

    if hydrated and _has_any_media(_media_counts(hydrated)):
        _store_hydration_metadata(hydrated, stage="wait_for_hydration", original=original, final=_snapshot_attachment_state(hydrated))
        return hydrated

    if ("http" in content) or attachments:
        channel = getattr(message, "channel", None)
        mid = getattr(message, "id", None)
        if channel and mid and hasattr(channel, "fetch_message"):
            try:
                fetched = await channel.fetch_message(mid)
            except (discord.NotFound, discord.Forbidden) as e:
                log.debug("hydrate_message: fetch_message NotFound/Forbidden for %s: %s", mid, e)
            except discord.HTTPException as e:
                log.debug("hydrate_message: fetch_message HTTPException for %s: %s", mid, e)
            else:
                if _has_any_media(_media_counts(fetched)):
                    _store_hydration_metadata(fetched, stage="fetch_message", original=original, final=_snapshot_attachment_state(fetched))
                    return fetched

    _store_hydration_metadata(message, stage=("wait_for_hydration" if hydrated else "skipped_unusable"), original=original, final=_snapshot_attachment_state(message))
    return message

def collect_media_items(
    message: discord.Message,
    bot: discord.Client,
    context: GuildScanContext,
) -> list[MediaWorkItem]:
    snapshots = getattr(message, "message_snapshots", None) or []
    snapshot = snapshots[0] if snapshots else None

    attachments = list(getattr(message, "attachments", None) or []) or list(getattr(snapshot, "attachments", None) or [])
    embeds = list(getattr(message, "embeds", None) or []) or list(getattr(snapshot, "embeds", None) or [])
    stickers = list(getattr(message, "stickers", None) or []) or list(getattr(snapshot, "stickers", None) or [])

    message_id = getattr(message, "id", None)
    message_channel = getattr(message, "channel", None)
    message_channel_id = getattr(message_channel, "id", None)
    message_guild = getattr(message, "guild", None)
    message_guild_id = getattr(message_guild, "id", None)

    snapshot_id = getattr(snapshot, "id", None) if snapshot else None
    snapshot_channel_id = getattr(snapshot, "channel_id", None) if snapshot else None
    snapshot_guild_id = getattr(snapshot, "guild_id", None) if snapshot else None

    # Signed URLs present in message content (used to override unsigned attachment URLs).
    content = getattr(message, "content", "") or ""
    signed_from_content = _extract_signed_discord_urls(content)
    signed_filename_map = _map_signed_urls_to_filenames(signed_from_content)

    # Carry hydration breadcrumbs for observability.
    h = _get_hydration_metadata(message)
    base_meta: dict[str, Any] = {}
    if h.get("stage"):
        base_meta["hydration_stage"] = h["stage"]
    if h.get("hydrated_urls"):
        base_meta["hydrated_urls"] = list(h["hydrated_urls"])
    if signed_from_content:
        base_meta["signed_content_urls"] = list(signed_from_content)

    items: list[MediaWorkItem] = []
    seen: set[str] = set()
    attachments_by_filename: dict[str, discord.Attachment] = {}

    for idx, a in enumerate(attachments):
        filename = getattr(a, "filename", None)
        proxy_url = getattr(a, "proxy_url", None)
        raw_url = getattr(a, "url", None)

        primary, fallbacks = _choose_best_attachment_url(
            filename, proxy_url, raw_url, signed_from_content, signed_filename_map
        )

        if not primary or not _is_http(primary):
            log.debug("collect: skip attachment (no usable URL). filename=%s proxy=%s raw=%s", filename, proxy_url, raw_url)
            continue
        if primary in seen:
            continue
        seen.add(primary)
        if filename:
            attachments_by_filename.setdefault(filename, a)

        ext = os.path.splitext(filename or "")[1]
        meta = {
            **base_meta,
            "size": getattr(a, "size", None),
            "attachment_id": getattr(a, "id", None),
            "message_id": message_id or snapshot_id,
            "channel_id": message_channel_id or snapshot_channel_id,
            "guild_id": message_guild_id or snapshot_guild_id,
        }
        if proxy_url:
            meta["proxy_url"] = str(proxy_url)
        if raw_url:
            meta["original_url"] = str(raw_url)
        if fallbacks:
            meta["fallback_urls"] = list(fallbacks)
        cache_hint = getattr(a, "hash", None)
        if cache_hint:
            meta["cache_key"] = f"hash::{cache_hint}"

        log.debug("collect: ATTACHMENT using %s (file=%s, fallbacks=%s)", primary, filename, bool(fallbacks))
        items.append(
            MediaWorkItem(
                source="attachment",
                label=filename or primary,
                url=primary,
                ext_hint=ext or None,
                metadata=meta,
                attachment=a,
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
            # Skip unsigned Discord CDN embed URLs (they 404); allow signed or non-Discord.
            if _is_discord_host(resolved) and not _is_signed_discord_media_url(resolved):
                log.debug("collect: skip unsigned discord EMBED %s", resolved)
                continue
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
        s_url = getattr(sticker, "url", None)
        if not _is_http(s_url):
            continue
        fmt = getattr(getattr(sticker, "format", None), "name", None)
        label = getattr(sticker, "name", None) or s_url
        ext = f".{fmt.lower()}" if fmt else None
        items.append(
            MediaWorkItem(
                source="sticker",
                label=label,
                url=str(s_url),
                ext_hint=ext,
                metadata={**base_meta, "sticker_format": (fmt or "").lower()},
            )
        )

    for src in (message, snapshot):
        text = getattr(src, "content", None) if src is not None else None
        if not text:
            continue
        for u in _extract_urls(text, limit=20):
            if u in seen:
                continue
            parsed = urlparse(u)
            domain = parsed.netloc.lower()
            tenor = domain == "tenor.com" or domain.endswith(".tenor.com")
            if tenor and not context.tenor_allowed:
                continue
            ext = os.path.splitext(parsed.path)[1].lower()
            if not tenor and ext not in ALLOWED_CONTENT_EXTS:
                continue
            # Skip unsigned Discord CDN URLs from plain content; prefer signed ones we already recorded.
            if _is_discord_host(u) and not _is_signed_discord_media_url(u):
                continue
            seen.add(u)
            items.append(
                MediaWorkItem(
                    source="content",
                    label=u,
                    url=u,
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
        # Prefer proxy if present (Discord usually sets this to a signed URL).
        if proxy_url:
            urls.append(str(proxy_url))
            return
        if url:
            s = str(url)
            urls.append(s)

    v = getattr(embed, "video", None)
    if v:
        add(getattr(v, "proxy_url", None), getattr(v, "url", None))
    i = getattr(embed, "image", None)
    if i:
        add(getattr(i, "proxy_url", None), getattr(i, "url", None))
    t = getattr(embed, "thumbnail", None)
    if t:
        add(getattr(t, "proxy_url", None), getattr(t, "url", None))

    return urls

def _resolve_embed_url(candidate: str, attachments_by_filename: dict[str, discord.Attachment]) -> str | None:
    parsed = urlparse(candidate)
    if parsed.scheme != "attachment":
        return candidate
    filename = parsed.path.lstrip("/")
    if not filename:
        return None
    a = attachments_by_filename.get(filename)
    if not a:
        return None
    # Prefer proxy/url from the resolved attachment entry.
    for attr in ("proxy_url", "url"):
        resolved = getattr(a, attr, None)
        if resolved:
            return str(resolved)
    return None

__all__ = ["hydrate_message", "collect_media_items"]