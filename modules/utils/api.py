import asyncio
import random
import time
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

FERNET_KEY = os.getenv("FERNET_SECRET_KEY") 
fernet = Fernet(FERNET_KEY)

def _get_client(api_key: str) -> AsyncOpenAI:
    if api_key not in _clients:
        _clients[api_key] = AsyncOpenAI(api_key=api_key)
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
        raise Exception(
            "Your API key didn't work. This is likely because your organization"
            " hasn't added any credit to its OpenAI account. Even though the "
            "moderation model is free, OpenAI requires accounts to have valid "
            "payment details on file. To add credit, visit the [OpenAI Billing "
            "Overview](https://platform.openai.com/account/billing/overview) "
            "page and purchase at least $5 in credits. Once you've added a "
            "payment method and credits, your API key should function correctly."
        ) from exc

_lock = asyncio.Lock()
async def get_next_shared_api_key():
    global _working_keys, _non_working_keys
    async with _lock:
        now = time.monotonic()
        for k, ts in list(_quarantine.items()):
            if ts <= now:
                _quarantine.pop(k, None)
                _working_keys.append(k)
        if not _working_keys:
            _working_keys = await get_working_api_keys()
            random.shuffle(_working_keys)

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
                    await user.send(
                        "**⚠️ Your OpenAI API key failed a moderation check.**\n\n"
                        "This usually means your account doesn't have active billing or is temporarily rate-limited.\n\n"
                        "Please check your [OpenAI Billing Dashboard](https://platform.openai.com/account/billing/overview) "
                        "to ensure your account has payment info and at least $5 in credits.\n\n"
                        "Once that's resolved, your key will automatically start working again — no need to re-add it."
                    )
                except Exception:
                    pass  # User has DMs off or blocked the bot

    return affected_rows > 0


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
