from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
from typing import Any, Mapping
from urllib.parse import urlparse

import aiohttp
from aiohttp import ClientError, ClientSession, ClientTimeout
from discord.ext import commands

from modules.nsfw_scanner.constants import NSFW_SCANNER_DEFAULT_HEADERS
from modules.nsfw_scanner.custom_blocks.config import CustomBlockStreamConfig
from modules.nsfw_scanner.custom_blocks.service import (
    CustomBlockError,
    add_custom_block_from_bytes,
    delete_custom_block,
    list_custom_blocks,
)
from modules.utils.redis_stream import (
    RedisStreamConsumer,
    RedisStreamMessage,
    normalize_stream_fields,
)

_logger = logging.getLogger(__name__)


class CustomBlockStreamProcessor(RedisStreamConsumer):
    """Consume dashboard upload commands and persist them to Milvus."""

    def __init__(self, bot: commands.Bot, config: CustomBlockStreamConfig) -> None:
        super().__init__(config, logger=_logger, delete_after_ack=True)
        self._bot = bot
        self._response_stream = config.response_stream
        self._max_response_length = config.max_response_length
        self._max_image_bytes = config.max_image_bytes
        self._download_timeout = ClientTimeout(total=float(config.download_timeout))
        self._session: ClientSession | None = None

    async def handle_message(self, message: RedisStreamMessage) -> bool:  # noqa: D401 - documented in base
        payload = normalize_stream_fields(message.fields)
        try:
            response = await self._handle_payload(payload)
        except asyncio.CancelledError:  # type: ignore[name-defined]
            raise
        except Exception as exc:
            _logger.exception(
                "Custom block command failed for id=%s payload=%s",
                message.message_id,
                payload,
            )
            response = self._format_error_response(payload, exc)

        if response is not None and self.redis is not None:
            try:
                await self.redis.xadd(  # type: ignore[func-returns-value]
                    self._response_stream,
                    response,
                    maxlen=self._max_response_length,
                    approximate=True,
                )
            except Exception:
                _logger.exception("Failed to publish custom block response payload=%s", response)

        return True

    async def stop(self) -> None:  # noqa: D401 - documented in base
        await super().stop()
        if self._session is not None:
            try:
                await self._session.close()
            finally:
                self._session = None

    async def _handle_payload(self, payload: Mapping[str, Any]) -> dict[str, str] | None:
        action = (payload.get("action") or "add").strip().lower()
        request_id = str(payload.get("request_id") or "")
        guild_id = _coerce_int(payload.get("guild_id"))

        if guild_id is None:
            raise CustomBlockError("guild_id is required.")
        if not action:
            action = "add"

        if action == "add":
            image_bytes, source = await self._resolve_image_bytes(payload)
            uploader_id = _coerce_int(payload.get("uploaded_by"))
            label = payload.get("label")
            extra_metadata = self._parse_metadata(payload.get("metadata_json"))

            if source and "source" not in extra_metadata:
                extra_metadata["source"] = source
            elif not source:
                extra_metadata.setdefault("source", "dashboard-upload")

            vector_id = await add_custom_block_from_bytes(
                guild_id,
                image_bytes,
                uploaded_by=uploader_id,
                label=label,
                source=source,
                extra_metadata=extra_metadata,
            )

            return {
                "request_id": request_id,
                "status": "ok",
                "action": "add",
                "guild_id": str(guild_id),
                "vector_id": str(vector_id),
            }

        if action == "delete":
            vector_id = _coerce_int(payload.get("vector_id"))
            if vector_id is None:
                raise CustomBlockError("vector_id is required for delete.")
            deleted = await delete_custom_block(guild_id, vector_id)
            response = {
                "request_id": request_id,
                "status": "ok",
                "action": "delete",
                "guild_id": str(guild_id),
                "vector_id": str(vector_id),
                "label": str(deleted.get("label") or ""),
            }
            if deleted.get("not_found"):
                response["not_found"] = "true"
            return response

        if action == "list":
            entries = await list_custom_blocks(guild_id)
            payload_entries = [
                {
                    "vector_id": entry.get("vector_id"),
                    "label": entry.get("label"),
                    "uploaded_by": entry.get("uploaded_by"),
                    "uploaded_at": entry.get("uploaded_at"),
                    "source": entry.get("source"),
                }
                for entry in entries
            ]
            return {
                "request_id": request_id,
                "status": "ok",
                "action": "list",
                "guild_id": str(guild_id),
                "entries": json.dumps(payload_entries),
            }

        raise CustomBlockError(f"Unsupported custom block action '{action}'.")

    async def _resolve_image_bytes(
        self,
        payload: Mapping[str, Any],
    ) -> tuple[bytes, str | None]:
        base64_data = payload.get("image_base64")
        if base64_data:
            try:
                data = base64.b64decode(base64_data, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise CustomBlockError("image_base64 is not valid base64 data.") from exc
            if len(data) > self._max_image_bytes:
                raise CustomBlockError("Uploaded image exceeds the size limit.")
            return data, payload.get("image_url") or payload.get("source_url")

        image_url = (payload.get("image_url") or payload.get("source_url") or "").strip()
        if not image_url:
            raise CustomBlockError("image_url or image_base64 must be supplied.")

        parsed = urlparse(image_url)
        if parsed.scheme not in {"http", "https"}:
            raise CustomBlockError("image_url must use http or https.")

        data = await self._download(image_url)
        if len(data) > self._max_image_bytes:
            raise CustomBlockError("Downloaded image exceeds the configured size limit.")
        return data, image_url

    async def _download(self, url: str) -> bytes:
        session = await self._ensure_session()
        try:
            async with session.get(url, headers=NSFW_SCANNER_DEFAULT_HEADERS) as response:
                response.raise_for_status()
                data = await response.read()
        except ClientError as exc:
            raise CustomBlockError(f"Failed to download image: {exc}") from exc
        return data

    async def _ensure_session(self) -> ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._download_timeout)
        return self._session

    def _parse_metadata(self, raw: Any) -> dict[str, Any]:
        if not raw:
            return {}
        if isinstance(raw, Mapping):
            candidate = raw
        else:
            try:
                candidate = json.loads(str(raw))
            except json.JSONDecodeError as exc:
                raise CustomBlockError("metadata_json must be valid JSON.") from exc
        if not isinstance(candidate, Mapping):
            raise CustomBlockError("metadata_json must describe an object.")

        metadata: dict[str, Any] = {}
        reserved_keys = {
            "category",
            "custom_block",
            "guild_id",
            "uploaded_by",
            "uploaded_at",
            "label",
            "source",
        }
        for key, value in candidate.items():
            key_str = str(key)
            if key_str in reserved_keys:
                continue
            metadata[key_str] = value
        return metadata

    def _format_error_response(self, payload: Mapping[str, Any], exc: Exception) -> dict[str, str]:
        request_id = str(payload.get("request_id") or "")
        guild_id = payload.get("guild_id")
        action = payload.get("action") or "add"

        if isinstance(exc, CustomBlockError):
            message = str(exc) or "Custom block request failed."
        else:  # pragma: no cover - defensive fallback
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


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = ["CustomBlockStreamProcessor"]
