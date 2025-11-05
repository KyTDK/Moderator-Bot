from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from discord.ext import commands

from modules.faq.config import FAQStreamConfig
from modules.faq.models import FAQEntry
from modules.faq.service import (
    FAQLimitError,
    FAQEntryNotFoundError,
    FAQServiceError,
    add_faq_entry,
    delete_faq_entry,
    list_faq_entries,
)
from modules.utils.redis_stream import (
    RedisStreamConsumer,
    RedisStreamMessage,
    normalize_stream_fields,
)

_logger = logging.getLogger(__name__)


class FAQStreamProcessor(RedisStreamConsumer):
    """Process FAQ commands that arrive via Redis streams."""

    def __init__(self, bot: commands.Bot, config: FAQStreamConfig) -> None:
        super().__init__(config, logger=_logger, delete_after_ack=True)
        self._bot = bot
        self._response_stream = config.response_stream
        self._max_response_length = config.max_response_length

    async def handle_message(self, message: RedisStreamMessage) -> bool:  # noqa: D401 - documented in base
        payload = normalize_stream_fields(message.fields)
        try:
            response = await self._handle_payload(payload)
        except asyncio.CancelledError:  # type: ignore[name-defined]
            raise
        except Exception as exc:
            _logger.exception(
                "FAQ stream command failed for id=%s payload=%s",
                message.message_id,
                payload,
            )
            response = _format_error_response(payload, exc)

        if response is not None and self.redis is not None:
            try:
                await self.redis.xadd(  # type: ignore[func-returns-value]
                    self._response_stream,
                    response,
                    maxlen=self._max_response_length,
                    approximate=True,
                )
            except Exception:
                _logger.exception("Failed to publish FAQ response payload=%s", response)

        return True

    async def _handle_payload(self, payload: dict[str, Any]) -> dict[str, str] | None:
        action = (payload.get("action") or "").lower().strip()
        request_id = payload.get("request_id") or ""
        guild_id = _coerce_int(payload.get("guild_id"))

        if guild_id is None:
            raise FAQServiceError("guild_id is required")
        if not action:
            raise FAQServiceError("action is required")

        if action == "add":
            question = (payload.get("question") or "").strip()
            answer = (payload.get("answer") or "").strip()
            entry = await add_faq_entry(guild_id, question, answer)
            return {
                "request_id": request_id,
                "status": "ok",
                "action": action,
                "guild_id": str(guild_id),
                "entry_id": str(entry.entry_id),
            }

        if action == "delete":
            entry_id = _coerce_int(payload.get("entry_id"))
            if entry_id is None:
                raise FAQServiceError("entry_id is required for delete")
            await delete_faq_entry(guild_id, entry_id)
            return {
                "request_id": request_id,
                "status": "ok",
                "action": action,
                "guild_id": str(guild_id),
                "entry_id": str(entry_id),
            }

        if action == "list":
            entries = await list_faq_entries(guild_id)
            packed_entries = json.dumps([_entry_to_dict(entry) for entry in entries])
            return {
                "request_id": request_id,
                "status": "ok",
                "action": action,
                "guild_id": str(guild_id),
                "entries": packed_entries,
            }

        raise FAQServiceError(f"Unknown FAQ action '{action}'")


def _entry_to_dict(entry: FAQEntry) -> dict[str, Any]:
    return {
        "entry_id": entry.entry_id,
        "question": entry.question,
        "answer": entry.answer,
    }


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _format_error_response(payload: dict[str, Any], exc: Exception) -> dict[str, str]:
    request_id = payload.get("request_id") or ""
    action = payload.get("action") or ""
    guild_id = payload.get("guild_id")

    if isinstance(exc, FAQLimitError):
        message = f"FAQ limit reached ({exc.limit} entries for plan {exc.plan})"
    elif isinstance(exc, FAQEntryNotFoundError):
        message = f"FAQ entry {exc.entry_id} not found"
    else:
        message = str(exc) or exc.__class__.__name__

    response = {
        "request_id": request_id,
        "status": "error",
        "action": str(action),
        "error": message,
    }
    if guild_id is not None:
        response["guild_id"] = str(guild_id)
    return response
