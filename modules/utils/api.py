import openai
from openai import AsyncOpenAI
import itertools
from modules.utils import mysql

_api_key_cycle = None
_api_keys_list = []

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
    global _api_key_cycle, _api_keys_list
    if _api_key_cycle is None or not _api_keys_list:
        _api_keys_list = get_all_api_keys()
        if not _api_keys_list:
            return None
        _api_key_cycle = itertools.cycle(_api_keys_list)
    try:
        next_key = next(_api_key_cycle)
        # Check if we've completed a full cycle
        if next_key == _api_keys_list[0]:
            # Refresh the API keys list and cycle
            _api_keys_list = get_all_api_keys()
            if not _api_keys_list:
                _api_key_cycle = None
                return None
            _api_key_cycle = itertools.cycle(_api_keys_list)
            next_key = next(_api_key_cycle)
        return next_key
    except StopIteration:
        return None

def get_api_client(guild_id):
    api_key = get_user_api_key(guild_id)
    if not api_key:
        api_key = get_next_shared_api_key()
    if not api_key:
        return None
    return AsyncOpenAI(api_key=api_key)

def set_api_key_not_working(api_key):
    query = "UPDATE api_pool SET working = FALSE WHERE api_key = %s"
    _, affected_rows = mysql.execute_query(query, (api_key,))
    return affected_rows > 0

def set_api_key_working(api_key):
    query = "UPDATE api_pool SET working = TRUE WHERE api_key = %s"
    _, affected_rows = mysql.execute_query(query, (api_key,))
    return affected_rows > 0

def is_api_key_working(api_key: str) -> bool:
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