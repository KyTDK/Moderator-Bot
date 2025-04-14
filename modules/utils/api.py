import openai
from openai import AsyncOpenAI
from modules.utils import mysql
from cryptography.fernet import Fernet
import os
from dotenv import load_dotenv

load_dotenv()

from itertools import cycle

_working_cycle = None
_non_working_cycle = None
_working_keys_cache = []
_non_working_keys_cache = []

FERNET_KEY = os.getenv("FERNET_SECRET_KEY") 
fernet = Fernet(FERNET_KEY)

def check_openai_api_key(api_key):
    try:
        client = openai.OpenAI(api_key=api_key)
        client.models.list()
        return True
    except openai.AuthenticationError:
        return False

def get_user_api_key(guild_id):
    return mysql.get_settings(guild_id, "api-key")

def get_next_shared_api_key():
    global _working_cycle, _non_working_cycle
    global _working_keys_cache, _non_working_keys_cache

    # Refresh working keys if cache is empty
    if not _working_keys_cache:
        _working_keys_cache = get_working_api_keys()
        _working_cycle = cycle(_working_keys_cache) if _working_keys_cache else None

    if _working_cycle:
        try:
            return next(_working_cycle)
        except StopIteration:
            # Shouldn't normally hit this due to cycle, but reset just in case
            _working_keys_cache = []
            _working_cycle = None

    # Working pool is empty or exhausted — fall back to non-working
    if not _non_working_keys_cache:
        _non_working_keys_cache = get_non_working_api_keys()
        _non_working_cycle = cycle(_non_working_keys_cache) if _non_working_keys_cache else None

    if _non_working_cycle:
        try:
            return next(_non_working_cycle)
        except StopIteration:
            # Reset fallback cycle
            _non_working_keys_cache = []
            _non_working_cycle = None

    # No keys available
    return None

def get_api_client(guild_id):
    api_key = get_user_api_key(guild_id)
    encrypted_key = None
    if not api_key:
        encrypted_key = get_next_shared_api_key()
        api_key = fernet.decrypt(encrypted_key.encode()).decode()
    if not api_key:
        return None, None
    return AsyncOpenAI(api_key=api_key), encrypted_key

def set_api_key_not_working(api_key):
    if not api_key:
        return
    query = "UPDATE api_pool SET working = FALSE WHERE api_key = %s"
    _, affected_rows = mysql.execute_query(query, (api_key,))
    return affected_rows > 0

def set_api_key_working(api_key):
    if not api_key:
        return
    query = "UPDATE api_pool SET working = TRUE WHERE api_key = %s"
    _, affected_rows = mysql.execute_query(query, (api_key,))
    return affected_rows > 0

def is_api_key_working(api_key: str) -> bool:
    if not api_key:
        return
    query = "SELECT working FROM api_pool WHERE api_key = %s"
    result, _ = mysql.execute_query(query, (api_key,), fetch_one=True)
    return result is not None and result[0] == 1

def get_all_api_keys():
    query = """
        SELECT api_key
        FROM api_pool
        ORDER BY working DESC
    """
    result, _ = mysql.execute_query(query, fetch_all=True)
    if result:
        return [row[0] for row in result]
    return []

def get_working_api_keys():
    query = """
        SELECT api_key
        FROM api_pool
        WHERE working = TRUE
    """
    result, _ = mysql.execute_query(query, fetch_all=True)
    return [row[0] for row in result] if result else []

def get_non_working_api_keys():
    query = """
        SELECT api_key
        FROM api_pool
        WHERE working = FALSE
    """
    result, _ = mysql.execute_query(query, fetch_all=True)
    return [row[0] for row in result] if result else []

