from openai import AsyncOpenAI
from modules.utils import mysql
from cryptography.fernet import Fernet
import os
from dotenv import load_dotenv

load_dotenv()

_working_keys = []
_non_working_keys = []

FERNET_KEY = os.getenv("FERNET_SECRET_KEY") 
fernet = Fernet(FERNET_KEY)

async def check_openai_api_key(api_key):
    try:
        client = AsyncOpenAI(api_key=api_key)
        await client.moderations.create(
            model="omni-moderation-latest",
            input=[
                {"type": "text", "text": "This is a test message."}
            ]
        )
    except:
        raise Exception("Your API key didn't work. This is likely because your organization hasn't added any credit to its OpenAI account. Even though the moderation model is free, OpenAI requires accounts to have valid payment details on file. To add credit, visit the [OpenAI Billing Overview](https://platform.openai.com/account/billing/overview) page and purchase at least $5 in credits. Once you've added a payment method and credits, your API key should function correctly.")

async def get_next_shared_api_key():
    global _working_keys, _non_working_keys

    # If working keys are empty, refill
    if not _working_keys:
        _working_keys = await get_working_api_keys()

    if _working_keys:
        return _working_keys.pop(0)

    # If we’ve exhausted working keys, fall back to non-working
    if not _non_working_keys:
        _non_working_keys = await get_non_working_api_keys()

    if _non_working_keys:
        return _non_working_keys.pop(0)

    return None  # No keys available

async def get_api_client(guild_id):
    api_key = await mysql.get_settings(guild_id, "api-key")
    encrypted_key = None
    if not api_key:
        encrypted_key = await get_next_shared_api_key()
        api_key = fernet.decrypt(encrypted_key.encode()).decode()
    if not api_key:
        return None, None
    return AsyncOpenAI(api_key=api_key), encrypted_key

async def set_api_key_not_working(api_key):
    if not api_key:
        return
    query = "UPDATE api_pool SET working = FALSE WHERE api_key = %s"
    _, affected_rows = await mysql.execute_query(query, (api_key,))
    return affected_rows > 0

async def set_api_key_working(api_key):
    if not api_key:
        return
    query = "UPDATE api_pool SET working = TRUE WHERE api_key = %s"
    _, affected_rows = await mysql.execute_query(query, (api_key,))
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

