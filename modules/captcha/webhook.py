from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from typing import Any

from aiohttp import web
from discord.ext import commands

from .config import CaptchaWebhookConfig
from .models import (
    CaptchaCallbackPayload,
    CaptchaPayloadError,
    CaptchaProcessingError,
    CaptchaWebhookResult,
)
from .processor import CaptchaCallbackProcessor
from .sessions import CaptchaSessionStore

_logger = logging.getLogger(__name__)

class CaptchaWebhookServer:
    """Thin aiohttp wrapper that exposes the /captcha/callback endpoint."""

    def __init__(
        self,
        bot: commands.Bot,
        config: CaptchaWebhookConfig,
        session_store: CaptchaSessionStore,
    ) -> None:
         self._bot = bot
         self._config = config
         self._processor = CaptchaCallbackProcessor(bot, session_store)
         self._app: web.Application | None = None
         self._runner: web.AppRunner | None = None
         self._site: web.BaseSite | None = None
         self._started = asyncio.Event()

    @property
    def started(self) -> bool:
        return self._started.is_set()

    async def start(self) -> None:
        if not self._config.enabled:
            _logger.info("Captcha webhook is disabled; skipping startup")
            return
        if self._runner is not None:
            return

        self._app = web.Application()
        self._app.add_routes([web.post("/captcha/callback", self._handle_callback)])

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()

        self._site = web.TCPSite(self._runner, self._config.host, self._config.port)
        await self._site.start()

        self._started.set()
        _logger.info(
            "Captcha webhook listening on %s:%s", self._config.host, self._config.port
        )

    async def stop(self) -> None:
        self._started.clear()

        if self._site is not None:
            await self._site.stop()
            self._site = None

        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

        self._app = None

    async def _handle_callback(self, request: web.Request) -> web.Response:
        body = await request.read()

        if not self._is_authorized(request, body):
            return web.json_response({"error": "unauthorized"}, status=401)

        try:
            payload_dict = await request.json(loads=self._json_loads)
        except json.JSONDecodeError:
            payload_dict = self._json_loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
             return web.json_response({"error": "invalid_json"}, status=400)

        try:
            payload = CaptchaCallbackPayload.from_mapping(payload_dict)
        except CaptchaPayloadError as exc:
            return web.json_response(
                {"error": "invalid_payload", "message": str(exc)},
                status=400,
            )

        try:
            result = await self._processor.process(payload)
        except CaptchaProcessingError as exc:
            _logger.info(
                "Captcha callback failed for guild %s user %s: %s",
                payload.guild_id,
                payload.user_id,
                exc,
            )
            return web.json_response(
                {"error": exc.code, "message": exc.message},
                status=exc.http_status,
            )
        except Exception:
            _logger.exception(
                "Unexpected error while processing captcha callback for guild %s user %s",
                payload.guild_id,
                payload.user_id,
            )
            return web.json_response({"error": "internal_error"}, status=500)

        return web.json_response(_serialize_result(result))

    def _is_authorized(self, request: web.Request, body: bytes) -> bool:
        if self._config.shared_secret:
            signature = (request.headers.get("X-Signature") or "").strip()
            expected = hmac.new(
                self._config.shared_secret,
                body,
                hashlib.sha256,
            ).hexdigest()
            if signature and hmac.compare_digest(signature.lower(), expected.lower()):
                return True
            return False

        if not self._config.token:
             return True
 
        auth_header = request.headers.get("Authorization") or ""
        token = _extract_token(auth_header)
        if token and token == self._config.token:
            return True

        token_query = request.query.get("token")
        if token_query and token_query == self._config.token:
            return True

        return False

    @staticmethod
    def _json_loads(data: str) -> Any:
        return json.loads(data)

def _serialize_result(result: CaptchaWebhookResult) -> dict[str, Any]:
    response = {
        "status": result.status,
        "roles_applied": result.roles_applied,
    }
    if result.message:
        response["message"] = result.message
    return response


def _extract_token(header_value: str) -> str | None:
    header_value = header_value.strip()
    if not header_value:
        return None

    if " " in header_value:
        scheme, value = header_value.split(" ", 1)
        if scheme.lower() in {"bot", "bearer", "token", "authorization"}:
            return value.strip()
    return header_value
