import asyncio
import random
import time
from dataclasses import dataclass
from typing import Mapping

import httpx
from openai import AsyncOpenAI

from modules.utils import mysql
from cryptography.fernet import Fernet
import os
from dotenv import load_dotenv

from modules.utils.discord_utils import safe_get_user

load_dotenv()

_working_keys = []
_non_working_keys = []
_quarantine: dict[str, float] = {}
_clients = {}
_rate_limit_state: dict[str, "RateLimitState"] = {}


class APIKeyValidationError(Exception):
    """Error raised when an API key fails the validation check."""

    __slots__ = ("translation_key", "fallback", "placeholders")

    def __init__(
        self,
        *,
        translation_key: str,
        fallback: str,
        placeholders: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(fallback)
        self.translation_key = translation_key
        self.fallback = fallback
        self.placeholders = placeholders or {}

FERNET_KEY = os.getenv("FERNET_SECRET_KEY") 
fernet = Fernet(FERNET_KEY)


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


_OPENAI_TIMEOUT = httpx.Timeout(
    connect=_float_env("OPENAI_CONNECT_TIMEOUT", 10.0),
    read=_float_env("OPENAI_READ_TIMEOUT", 45.0),
    write=_float_env("OPENAI_WRITE_TIMEOUT", 45.0),
    pool=_float_env("OPENAI_POOL_TIMEOUT", 45.0),
)

_RATE_LIMIT_BASE_COOLDOWN = _float_env(
    "OPENAI_RATE_LIMIT_BASE_COOLDOWN",
    _float_env("OPENAI_RATE_LIMIT_COOLDOWN", 180.0),
)
_RATE_LIMIT_MAX_COOLDOWN = _float_env("OPENAI_RATE_LIMIT_MAX_COOLDOWN", 3600.0)
_RATE_LIMIT_BACKOFF_DECAY = _float_env("OPENAI_RATE_LIMIT_BACKOFF_DECAY", 900.0)


@dataclass(slots=True)
class RateLimitState:
    strike_count: int
    last_failure: float


@dataclass(slots=True)
class RateLimitPenalty:
    cooldown_seconds: float
    strike_count: int

def _get_client(api_key: str) -> AsyncOpenAI:
    if api_key not in _clients:
        _clients[api_key] = AsyncOpenAI(
            api_key=api_key,
            max_retries=0,
            timeout=_OPENAI_TIMEOUT,
        )
    return _clients[api_key]

async def check_openai_api_key(api_key):
    try:
        client = AsyncOpenAI(api_key=api_key)
        await client.moderations.create(
            model="omni-moderation-latest",
            input=[
                {"type": "text", "text": "This is a test message."}
            ]
        )
    except Exception as exc:
        raise APIKeyValidationError(
            translation_key="modules.utils.api.key_validation.billing_required",
            fallback=(
                "Your API key didn't work. This is likely because your organization "
                "hasn't added any credit to its OpenAI account. Even though the "
                "moderation model is free, OpenAI requires accounts to have valid "
                "payment details on file. To add credit, visit the [OpenAI Billing "
                "Overview](https://platform.openai.com/account/billing/overview) "
                "page and purchase at least $5 in credits. Once you've added a "
                "payment method and credits, your API key should function correctly."
            ),
        ) from exc

_lock = asyncio.Lock()
async def get_next_shared_api_key():
    global _working_keys, _non_working_keys
    async with _lock:
        now = time.monotonic()
        for k, ts in list(_quarantine.items()):
            if ts <= now:
                _quarantine.pop(k, None)
                state = _rate_limit_state.get(k)
                if state is not None and now - state.last_failure > _RATE_LIMIT_BACKOFF_DECAY:
                    _rate_limit_state.pop(k, None)
                if k not in _working_keys:
                    _working_keys.append(k)
        if not _working_keys:
            _working_keys = await get_working_api_keys()
            random.shuffle(_working_keys)

        if _working_keys:
            _working_keys = [
                k for k in _working_keys if _quarantine.get(k, 0) <= now
            ]

        if _working_keys:
            return _working_keys.pop(0)

        if not _non_working_keys:
            _non_working_keys = await get_non_working_api_keys()

        if _non_working_keys:
            return _non_working_keys.pop(0)

    return None

async def get_api_client(guild_id):
    """Return an OpenAI client from the shared API pool only.

    Per-guild API keys are not used. If no working pooled key is available,
    returns (None, None).
    """
    encrypted_key = await get_next_shared_api_key()
    if encrypted_key is None:
        return None, None
    api_key = fernet.decrypt(encrypted_key.encode()).decode()
    client = _get_client(api_key)
    return client, encrypted_key

async def set_api_key_working(api_key):
    if not api_key:
        return
    async with _lock:
        _quarantine.pop(api_key, None)
        _rate_limit_state.pop(api_key, None)
        if api_key in _non_working_keys:
            _non_working_keys.remove(api_key)
        if api_key not in _working_keys:
            _working_keys.append(api_key)
    query = "UPDATE api_pool SET working = TRUE WHERE api_key = %s"
    _, affected_rows = await mysql.execute_query(query, (api_key,))
    return affected_rows > 0

async def set_api_key_not_working(api_key, bot=None):
    if not api_key:
        return

    async with _lock:
        _quarantine[api_key] = time.monotonic() + 60
        _rate_limit_state.pop(api_key, None)
        if api_key in _working_keys:
            _working_keys.remove(api_key)
        if api_key not in _non_working_keys:
            _non_working_keys.append(api_key)

    # Mark key as non-working in DB
    query = "UPDATE api_pool SET working = FALSE WHERE api_key = %s"
    _, affected_rows = await mysql.execute_query(query, (api_key,))

    if bot:
        # Find the user who owns this API key
        query = "SELECT user_id FROM api_pool WHERE api_key = %s"
        result, _ = await mysql.execute_query(query, (api_key,), fetch_one=True)

        if result:
            user_id = result[0]
            user = await safe_get_user(bot, user_id)
            if user:
                try:
                    translator = getattr(bot, "translate", None)
                    fallback = (
                        "**⚠️ Your OpenAI API key failed a moderation check.**\n\n"
                        "This usually means your account doesn't have active billing or is temporarily rate-limited.\n\n"
                        "Please check your [OpenAI Billing Dashboard](https://platform.openai.com/account/billing/overview) "
                        "to ensure your account has payment info and at least $5 in credits.\n\n"
                        "Once that's resolved, your key will automatically start working again - no need to re-add it."
                    )
                    message = (
                        translator("modules.utils.api.key_failed_notice", fallback=fallback)
                        if callable(translator)
                        else fallback
                    )
                    await user.send(message)
                except Exception:
                    pass  # User has DMs off or blocked the bot
                    pass  # User has DMs off or blocked the bot

    return affected_rows > 0


async def mark_api_key_rate_limited(
    api_key: str,
    cooldown: float | None = None,
) -> RateLimitPenalty | None:
    if not api_key:
        return None
    async with _lock:
        now = time.monotonic()
        base_cooldown = cooldown if cooldown is not None else _RATE_LIMIT_BASE_COOLDOWN
        if base_cooldown <= 0:
            return None
        state = _rate_limit_state.get(api_key)
        if state is not None and now - state.last_failure <= _RATE_LIMIT_BACKOFF_DECAY:
            strike_count = state.strike_count + 1
        else:
            strike_count = 1
        effective_cooldown = base_cooldown * (2 ** (strike_count - 1))
        if effective_cooldown > _RATE_LIMIT_MAX_COOLDOWN:
            effective_cooldown = _RATE_LIMIT_MAX_COOLDOWN
        expires_at = now + effective_cooldown
        existing_expiry = _quarantine.get(api_key)
        if existing_expiry and existing_expiry > expires_at:
            expires_at = existing_expiry
        _quarantine[api_key] = expires_at
        if api_key in _working_keys:
            _working_keys.remove(api_key)
        _rate_limit_state[api_key] = RateLimitState(
            strike_count=strike_count,
            last_failure=now,
        )
    return RateLimitPenalty(
        cooldown_seconds=effective_cooldown,
        strike_count=strike_count,
    )


async def is_api_key_working(api_key: str) -> bool:
    if not api_key:
        return
    query = "SELECT working FROM api_pool WHERE api_key = %s"
    result, _ = await mysql.execute_query(query, (api_key,), fetch_one=True)
    return result is not None and result[0] == 1

async def get_working_api_keys():
    query = """
        SELECT api_key
        FROM api_pool
        WHERE working = TRUE
    """
    result, _ = await mysql.execute_query(query, fetch_all=True)
    return [row[0] for row in result] if result else []

async def get_non_working_api_keys():
    query = """
        SELECT api_key
        FROM api_pool
        WHERE working = FALSE
    """
    result, _ = await mysql.execute_query(query, fetch_all=True)
    return [row[0] for row in result] if result else []

async def is_guild_in_api_pool(guild_id: int) -> bool:
    result, _ = await mysql.execute_query(
        "SELECT 1 FROM api_pool WHERE guild_id = %s LIMIT 1",
        (guild_id,),
        fetch_one=True
    )
    return result is not None
