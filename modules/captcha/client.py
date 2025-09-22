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
    "CaptchaDeliveryPreferences",
    "CaptchaGuildConfig",
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


@dataclass(slots=True)
class CaptchaDeliveryPreferences:
    method: str
    requires_login: bool
    embed_channel_id: int | None


@dataclass(slots=True)
class CaptchaGuildConfig:
    guild_id: int
    delivery: CaptchaDeliveryPreferences
    provider: str | None
    provider_label: str | None


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
        callback_url: str | None = None,
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
        if callback_url:
            payload["callbackUrl"] = callback_url

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

    async def fetch_guild_config(self, guild_id: int) -> CaptchaGuildConfig:
        if not self.is_configured:
            raise CaptchaApiError("Captcha API token or base URL is not configured.")

        session = await self._ensure_session()
        url = self._base_url
        headers = {"Authorization": f"Bot {self._api_token}"}

        try:
            async with session.get(url, params={"gid": str(guild_id)}, headers=headers) as resp:
                data = await _read_json(resp)
        except aiohttp.ClientError as exc:
            raise CaptchaApiError("Failed to contact captcha API.") from exc

        if resp.status == 404:
            raise CaptchaNotAvailableError(
                "Captcha is not enabled for this guild.",
                status=resp.status,
            )
        if resp.status == 401:
            raise CaptchaApiError("Captcha API token is invalid.", status=resp.status)
        if resp.status >= 400:
            raise CaptchaApiError(
                f"Captcha API returned HTTP {resp.status}.",
                status=resp.status,
            )

        if not isinstance(data, dict):
            raise CaptchaApiError("Unexpected response from captcha API.")

        delivery_raw = data.get("delivery")
        if isinstance(delivery_raw, dict):
            method = str(delivery_raw.get("method", "dm") or "dm").lower()
            requires_login = bool(delivery_raw.get("requiresLogin"))
            embed_channel_raw = delivery_raw.get("embedChannelId")
        else:
            method = "dm"
            requires_login = False
            embed_channel_raw = None

        embed_channel_id: int | None
        try:
            embed_channel_id = int(embed_channel_raw) if embed_channel_raw is not None else None
        except (TypeError, ValueError):
            embed_channel_id = None

        captcha_raw = data.get("captcha")
        provider: str | None = None
        provider_label: str | None = None
        if isinstance(captcha_raw, dict):
            raw_provider = captcha_raw.get("provider")
            raw_label = captcha_raw.get("label") or captcha_raw.get("providerLabel")
            provider = str(raw_provider) if isinstance(raw_provider, str) else None
            provider_label = (
                str(raw_label)
                if isinstance(raw_label, str)
                else (provider.title() if provider else None)
            )

        delivery = CaptchaDeliveryPreferences(
            method=method,
            requires_login=requires_login,
            embed_channel_id=embed_channel_id,
        )

        return CaptchaGuildConfig(
            guild_id=int(data.get("guildId", guild_id) or guild_id),
            delivery=delivery,
            provider=provider,
            provider_label=provider_label,
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
