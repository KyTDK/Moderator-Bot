from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiohttp

__all__ = [
    "CaptchaApiClient",
    "CaptchaApiError",
    "CaptchaNotAvailableError",
    "CaptchaStartResponse",
]

class CaptchaApiError(RuntimeError):
    """Raised when the captcha API reports an error."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

class CaptchaNotAvailableError(CaptchaApiError):
    """Raised when captcha is not available for the guild."""

@dataclass(slots=True)
class CaptchaStartResponse:
    token: str
    guild_id: int
    user_id: int
    verification_url: str
    expires_at: datetime
    state: str | None

class CaptchaApiClient:
    """Lightweight HTTP client for the captcha backend."""

    def __init__(
        self,
        base_url: str,
        api_token: str | None,
        *,
        timeout: float = 10.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_token = api_token
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()

    @property
    def is_configured(self) -> bool:
        return bool(self._api_token and self._base_url)

    async def close(self) -> None:
        async with self._lock:
            if self._session is not None:
                await self._session.close()
                self._session = None

    async def start_session(
        self,
        guild_id: int,
        user_id: int,
        *,
        state: str | None = None,
        redirect: str | None = None,
    ) -> CaptchaStartResponse:
        if not self.is_configured:
            raise CaptchaApiError("Captcha API token or base URL is not configured.")

        payload: dict[str, Any] = {
            "guildId": str(guild_id),
            "userId": str(user_id),
        }
        if state:
            payload["state"] = state
        if redirect:
            payload["redirect"] = redirect

        session = await self._ensure_session()
        url = f"{self._base_url}/start"
        headers = {"Authorization": f"Bot {self._api_token}"}

        try:
            async with session.post(url, json=payload, headers=headers) as resp:
                data = await _read_json(resp)
        except aiohttp.ClientError as exc:
            raise CaptchaApiError("Failed to contact captcha API.") from exc

        if isinstance(data, dict):
            message = data.get("error") or data.get("message")
        else:
            message = None

        if resp.status == 404:
            raise CaptchaNotAvailableError(
                message or "Captcha is not enabled for this guild.",
                status=resp.status,
            )
        if resp.status == 401:
            raise CaptchaApiError("Captcha API token is invalid.", status=resp.status)
        if resp.status >= 400:
            raise CaptchaApiError(
                message or f"Captcha API returned HTTP {resp.status}.",
                status=resp.status,
            )

        if not isinstance(data, dict):
            raise CaptchaApiError("Unexpected response from captcha API.")

        try:
            token = data["token"]
            verification_url = data["verificationUrl"]
            expires_at_ms = data["expiresAt"]
        except KeyError as exc:
            raise CaptchaApiError(f"Captcha API response missing field {exc.args[0]}") from exc

        state_value = data.get("state")
        expires_at = _coerce_datetime(expires_at_ms)

        try:
            response_guild = int(data.get("guildId", guild_id))
            response_user = int(data.get("userId", user_id))
        except (TypeError, ValueError) as exc:
            raise CaptchaApiError("Invalid guildId or userId in captcha response.") from exc

        return CaptchaStartResponse(
            token=str(token),
            guild_id=response_guild,
            user_id=response_user,
            verification_url=str(verification_url),
            expires_at=expires_at,
            state=state_value if isinstance(state_value, str) else None,
        )

    async def _ensure_session(self) -> aiohttp.ClientSession:
        async with self._lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(timeout=self._timeout)
            return self._session


async def _read_json(response: aiohttp.ClientResponse) -> Any:
    try:
        return await response.json()
    except aiohttp.ContentTypeError:
        raise CaptchaApiError(
            f"Captcha API returned non-JSON response (status {response.status})."
        ) from None


def _coerce_datetime(value: Any) -> datetime:
    try:
        millis = float(value)
    except (TypeError, ValueError) as exc:
        raise CaptchaApiError("Invalid expiresAt value from captcha API.") from exc
    seconds = millis / 1000.0
    return datetime.fromtimestamp(seconds, tz=timezone.utc)
