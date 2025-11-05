from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Optional, Sequence

import discord

from modules.faq import storage, vector_store
from modules.faq.models import FAQEntry
from modules.utils.log_channel import DeveloperLogField, log_to_developer_channel

log = logging.getLogger(__name__)

__all__ = [
    "_VECTOR_BACKFILL_RETRY_DELAY",
    "_pending_vector_backfill",
    "_vector_backfill_task",
    "_backfill_attempts",
    "configure_developer_logging",
    "_queue_vector_backfill",
    "_remove_from_backfill",
]

_VECTOR_BACKFILL_RETRY_DELAY = 5.0
_pending_vector_backfill: set[tuple[int, int]] = set()
_vector_backfill_task: asyncio.Task[None] | None = None
_backfill_attempts: defaultdict[tuple[int, int], int] = defaultdict(int)

_developer_log_bot: Optional[discord.Client] = None
_developer_log_context: str = "faq.vector"
_developer_log_mention: Optional[str] = None
_developer_log_cooldown: float = 60.0
_developer_log_last_unavailable: float = 0.0


def configure_developer_logging(
    *,
    bot: Optional[discord.Client],
    context: str | None = None,
    mention: str | None = None,
    cooldown: float | None = None,
) -> None:
    """Configure developer log routing for FAQ service issues."""

    global _developer_log_bot, _developer_log_context, _developer_log_mention, _developer_log_cooldown
    _developer_log_bot = bot
    if context:
        _developer_log_context = context
    if mention is not None:
        _developer_log_mention = mention
    if cooldown is not None:
        _developer_log_cooldown = max(0.0, float(cooldown))


def _queue_vector_backfill(entry: FAQEntry) -> None:
    """Schedule a background attempt to add the FAQ entry to the vector store."""

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop (e.g., during import); defer until later.
        return

    key = (entry.guild_id, entry.entry_id)
    _pending_vector_backfill.add(key)
    # Initialize attempt counter if this is the first time we've seen the entry.
    _backfill_attempts.setdefault(key, 0)
    _ensure_vector_backfill_task(loop)


def _remove_from_backfill(entry: FAQEntry) -> None:
    key = (entry.guild_id, entry.entry_id)
    _pending_vector_backfill.discard(key)
    _backfill_attempts.pop(key, None)


def _ensure_vector_backfill_task(loop: asyncio.AbstractEventLoop) -> None:
    global _vector_backfill_task

    if _vector_backfill_task is not None and not _vector_backfill_task.done():
        return

    _vector_backfill_task = loop.create_task(_process_vector_backfill())
    _vector_backfill_task.add_done_callback(_handle_backfill_task_done)


def _handle_backfill_task_done(task: asyncio.Task[None]) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception:  # pragma: no cover - defensive logging
        log.exception("FAQ vector backfill task failed")
        if _pending_vector_backfill:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            _ensure_vector_backfill_task(loop)


def _truncate(value: str | None, *, limit: int = 1024) -> str:
    text = value or ""
    if len(text) <= limit:
        return text or "(empty)"
    return f"{text[: limit - 3].rstrip()}..."


def _format_exception(exc: Exception) -> str:
    return f"{exc.__class__.__name__}: {exc}"


def _entry_context_fields(
    entry: FAQEntry | None,
    *,
    guild_id: int | None = None,
    entry_id: int | None = None,
    attempts: int | None = None,
) -> list[DeveloperLogField]:
    fields: list[DeveloperLogField] = []
    resolved_guild_id = entry.guild_id if entry is not None else guild_id
    resolved_entry_id = entry.entry_id if entry is not None else entry_id
    if resolved_guild_id is not None:
        fields.append(DeveloperLogField("Guild", str(resolved_guild_id)))
    if resolved_entry_id is not None:
        fields.append(DeveloperLogField("Entry ID", str(resolved_entry_id)))
    if entry is not None:
        fields.append(
            DeveloperLogField(
                "Question",
                _truncate(entry.question, limit=1024),
                inline=False,
            )
        )
        fields.append(
            DeveloperLogField(
                "Answer",
                _truncate(entry.answer, limit=1024),
                inline=False,
            )
        )
    effective_attempts = attempts
    if effective_attempts is None and resolved_guild_id is not None and resolved_entry_id is not None:
        effective_attempts = _backfill_attempts.get((resolved_guild_id, resolved_entry_id))
    if effective_attempts:
        fields.append(DeveloperLogField("Backfill Attempts", str(effective_attempts)))
    fields.append(DeveloperLogField("Pending Entries", str(len(_pending_vector_backfill))))
    return fields


def _build_vector_debug_field() -> DeveloperLogField:
    info = vector_store.get_debug_info()
    lines = []
    for key, value in sorted(info.items()):
        lines.append(f"{key} = {value}")
    payload = "\n".join(lines) if lines else "No debug data available"
    return DeveloperLogField("Vector Store Debug", _truncate(payload, limit=1024), inline=False)


async def _log_backfill_event(
    summary: str,
    *,
    severity: str,
    entry: FAQEntry | None = None,
    guild_id: int | None = None,
    entry_id: int | None = None,
    attempts: int | None = None,
    description: str | None = None,
    extra_fields: Sequence[DeveloperLogField] | None = None,
) -> None:
    """Send a developer log entry if logging is configured."""

    if _developer_log_bot is None:
        return

    fields: list[DeveloperLogField] = _entry_context_fields(
        entry,
        guild_id=guild_id,
        entry_id=entry_id,
        attempts=attempts,
    )
    if extra_fields:
        fields.extend(extra_fields)
    fields.append(_build_vector_debug_field())

    try:
        await log_to_developer_channel(
            _developer_log_bot,
            summary=summary,
            severity=severity,
            description=description,
            fields=fields,
            context=_developer_log_context,
            mention=_developer_log_mention,
            timestamp=True,
        )
    except Exception:  # pragma: no cover - defensive logging
        log.debug("Failed to dispatch developer log for FAQ backfill event", exc_info=True)


async def _log_vector_unavailable() -> None:
    """Throttle developer logs when the vector store is unavailable."""

    if _developer_log_bot is None:
        return

    global _developer_log_last_unavailable
    now = time.monotonic()
    if _developer_log_cooldown > 0 and (now - _developer_log_last_unavailable) < _developer_log_cooldown:
        return

    _developer_log_last_unavailable = now
    await _log_backfill_event(
        "FAQ vector store unavailable",
        severity="warning",
        description="Vector store reported unavailable during FAQ backfill. Will retry entries while awaiting recovery.",
    )


async def _process_vector_backfill() -> None:
    while _pending_vector_backfill:
        if not vector_store.is_available():
            await _log_vector_unavailable()
            await asyncio.sleep(_VECTOR_BACKFILL_RETRY_DELAY)
            continue

        guild_id, entry_id = _pending_vector_backfill.pop()
        key = (guild_id, entry_id)
        attempts = _backfill_attempts[key] = _backfill_attempts.get(key, 0) + 1

        entry = await storage.fetch_entry(guild_id, entry_id)
        if entry is None:
            _backfill_attempts.pop(key, None)
            await _log_backfill_event(
                "FAQ backfill entry missing",
                severity="info",
                guild_id=guild_id,
                entry_id=entry_id,
                attempts=attempts,
                description="Entry was removed before vector backfill completed; dropping from retry queue.",
            )
            continue
        if entry.vector_id is not None:
            _backfill_attempts.pop(key, None)
            continue

        try:
            vector_id = await vector_store.add_entry(entry)
        except Exception as exc:  # pragma: no cover - defensive logging
            log.exception(
                "Failed to add FAQ vector for guild=%s entry=%s",
                guild_id,
                entry_id,
            )
            _pending_vector_backfill.add(key)
            await _log_backfill_event(
                "FAQ vector embedding failed",
                severity="error",
                entry=entry,
                attempts=attempts,
                description=f"{_format_exception(exc)}\nScheduling another attempt in {_VECTOR_BACKFILL_RETRY_DELAY:.1f}s.",
            )
            await asyncio.sleep(_VECTOR_BACKFILL_RETRY_DELAY)
            continue

        if vector_id is None:
            _pending_vector_backfill.add(key)
            await _log_backfill_event(
                "FAQ vector embedding deferred",
                severity="warning",
                entry=entry,
                attempts=attempts,
                description=f"Vector store returned no identifier; retrying in {_VECTOR_BACKFILL_RETRY_DELAY:.1f}s.",
            )
            await asyncio.sleep(_VECTOR_BACKFILL_RETRY_DELAY)
            continue

        entry.vector_id = vector_id
        try:
            await storage.update_vector_id(guild_id, entry_id, vector_id)
        except Exception as exc:  # pragma: no cover - defensive logging
            log.exception(
                "Failed to persist FAQ vector id for guild=%s entry=%s",
                guild_id,
                entry_id,
            )
            _pending_vector_backfill.add(key)
            await _log_backfill_event(
                "FAQ vector persistence failed",
                severity="error",
                entry=entry,
                attempts=attempts,
                description=f"{_format_exception(exc)}\nVector ID {vector_id} will be retried in {_VECTOR_BACKFILL_RETRY_DELAY:.1f}s.",
                extra_fields=[DeveloperLogField("Vector ID", str(vector_id))],
            )
            await asyncio.sleep(_VECTOR_BACKFILL_RETRY_DELAY)
            continue

        _backfill_attempts.pop(key, None)
        await asyncio.sleep(0)
