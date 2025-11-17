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
from modules.utils.log_channel import (
    DeveloperLogField,
    log_to_developer_channel,
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
            await self._report_command_failure(
                payload=payload,
                error=exc,
                message_id=message.message_id,
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
        action_raw = _get_payload_value(payload, "action")
        action = (action_raw or "add").strip().lower()
        request_id = str(_get_payload_value(payload, "request_id", "requestId") or "")
        guild_id = _coerce_int(_get_payload_value(payload, "guild_id", "guildId"))

        if guild_id is None:
            raise CustomBlockError("guild_id is required.")
        if not action:
            action = "add"

        if action == "add":
            image_bytes, source = await self._resolve_image_bytes(payload)
            uploader_id = _coerce_int(_get_payload_value(payload, "uploaded_by", "uploadedBy"))
            label = _get_payload_value(payload, "label")
            extra_metadata = self._parse_metadata(_get_payload_value(payload, "metadata_json", "metadataJson"))

            source = source or _get_payload_value(payload, "image_url", "imageUrl", "source_url", "sourceUrl")

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
            vector_id = _coerce_int(_get_payload_value(payload, "vector_id", "vectorId"))
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
            payload_entries = []
            for entry in entries:
                vector_id = entry.get("vector_id")
                uploaded_by = entry.get("uploaded_by")
                payload_entries.append(
                    {
                        "vector_id": str(vector_id) if vector_id is not None else None,
                        "label": entry.get("label"),
                        "uploaded_by": str(uploaded_by) if uploaded_by is not None else None,
                        "uploaded_at": entry.get("uploaded_at"),
                        "source": entry.get("source"),
                    }
                )
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
        base64_data = _get_payload_value(payload, "image_base64", "imageBase64")
        if base64_data:
            try:
                data = base64.b64decode(base64_data, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise CustomBlockError("image_base64 is not valid base64 data.") from exc
            if len(data) > self._max_image_bytes:
                raise CustomBlockError("Uploaded image exceeds the size limit.")
            source_hint = _get_payload_value(payload, "image_url", "imageUrl", "source_url", "sourceUrl")
            return data, source_hint

        image_url = (
            _get_payload_value(payload, "image_url", "imageUrl", "source_url", "sourceUrl") or ""
        ).strip()
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
        request_id = str(_get_payload_value(payload, "request_id", "requestId") or "")
        guild_id = _get_payload_value(payload, "guild_id", "guildId")
        action = _get_payload_value(payload, "action") or "add"

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

    async def _report_command_failure(
        self,
        *,
        payload: Mapping[str, Any],
        error: Exception,
        message_id: str,
    ) -> None:
        if self._bot is None:
            return

        try:
            guild_id = _get_payload_value(payload, "guild_id", "guildId")
            vector_id = _get_payload_value(payload, "vector_id", "vectorId")
            request_id = _get_payload_value(payload, "request_id", "requestId") or ""
            action = (_get_payload_value(payload, "action") or "add").strip().lower()
            error_message = str(error) or error.__class__.__name__
            payload_snapshot = json.dumps(
                {k: str(v) for k, v in payload.items()},
                ensure_ascii=False,
            )
            if len(payload_snapshot) > 1024:
                payload_snapshot = payload_snapshot[:1011] + "...\""

            fields = [
                DeveloperLogField(
                    name="Guild ID",
                    value=str(guild_id) if guild_id is not None else "unknown",
                    inline=True,
                ),
            ]
            if action:
                fields.append(
                    DeveloperLogField(
                        name="Action",
                        value=action,
                        inline=True,
                    )
                )
            if vector_id is not None:
                fields.append(
                    DeveloperLogField(
                        name="Vector ID",
                        value=str(vector_id),
                        inline=True,
                    )
                )
            if request_id:
                fields.append(
                    DeveloperLogField(
                        name="Request ID",
                        value=str(request_id),
                        inline=True,
                    )
                )
            fields.extend(
                [
                    DeveloperLogField(
                        name="Redis Message ID",
                        value=str(message_id),
                        inline=False,
                    ),
                    DeveloperLogField(
                        name="Error",
                        value=f"```{error_message}```",
                        inline=False,
                    ),
                    DeveloperLogField(
                        name="Payload",
                        value=f"```json\n{payload_snapshot}\n```",
                        inline=False,
                    ),
                ]
            )

            summary_action = action or "command"
            await log_to_developer_channel(
                self._bot,
                summary=f"Custom block {summary_action} failed",
                severity="error",
                description="Custom block stream command raised an exception.",
                fields=fields,
                context=f"custom_blocks.{summary_action}",
            )
        except Exception:  # pragma: no cover - defensive fallback
            _logger.exception("Failed to report custom block failure to developer log channel")


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _get_payload_value(payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


__all__ = ["CustomBlockStreamProcessor"]
