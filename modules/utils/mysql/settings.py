import copy
import json
from typing import Any

from modules.config.settings_schema import SETTINGS_SCHEMA

from .config import fernet
from .connection import execute_query
from .premium import is_accelerated

async def get_settings(
    guild_id: int,
    settings_key: str | list[str] | None = None,
):
    settings_row, _ = await execute_query(
        "SELECT settings_json FROM settings WHERE guild_id = %s",
        (guild_id,),
        fetch_one=True,
    )
    raw = json.loads(settings_row[0]) if settings_row else {}

    # Normalize requested keys
    if isinstance(settings_key, str):
        requested = [settings_key]
    elif isinstance(settings_key, list):
        requested = settings_key
    else:
        # No key provided: process all schema keys + any unknown keys found in raw
        requested = list(set(SETTINGS_SCHEMA.keys()) | set(raw.keys()))

    # Only check acceleration if any requested key is accelerated-only
    needs_accel = any(getattr(SETTINGS_SCHEMA.get(k), "accelerated", False) for k in requested)
    accelerated_enabled = await is_accelerated(guild_id=guild_id) if needs_accel else None

    def process_key(key: str) -> Any:
        schema = SETTINGS_SCHEMA.get(key)
        default = copy.deepcopy(getattr(schema, "default", None))
        encrypted = bool(getattr(schema, "encrypted", False))
        accelerated_only = bool(getattr(schema, "accelerated", False))

        value = raw.get(key, copy.deepcopy(default))

        # Override with default for accelerated-only settings if guild isn't accelerated
        if accelerated_only and (accelerated_enabled is False):
            value = copy.deepcopy(default)

        # Decrypt stored values when applicable
        if encrypted and value:
            value = fernet.decrypt(value.encode()).decode()

        # Type coercions / migrations
        if schema:
            if schema.type is bool and isinstance(value, str):
                value = value.lower() == "true"

            if schema.type == list[str]:
                if isinstance(value, str):
                    value = [value]
                elif not isinstance(value, list):
                    value = []
                value = [v for v in value if v != "none"]

            if key == "strike-actions":
                migrated: dict[str, list[str]] = {}
                if isinstance(value, dict):
                    for action_key, action_value in value.items():
                        if isinstance(action_value, list):
                            migrated[action_key] = action_value
                        elif isinstance(action_value, tuple):
                            action, duration = action_value
                            migrated[action_key] = [f"{action}:{duration}" if duration else action]
                        else:
                            migrated[action_key] = [str(action_value)]
                    value = migrated

        return value

    result = {k: process_key(k) for k in requested}

    # If a single key was requested, return just that value
    if settings_key is not None and len(requested) == 1:
        return result[requested[0]]

    return result

async def update_settings(guild_id: int, settings_key: str, settings_value):
    settings = await get_settings(guild_id)

    schema = SETTINGS_SCHEMA.get(settings_key)
    encrypt_current = schema.encrypted if schema else False

    if settings_value is None:
        changed = settings.pop(settings_key, None) is not None
    else:
        if settings_key == "strike-actions" and isinstance(settings_value, dict):
            converted: dict[str, list[str]] = {}
            for key, value in settings_value.items():
                if isinstance(value, list):
                    converted[key] = value
                elif isinstance(value, tuple):
                    action, duration = value
                    converted[key] = [f"{action}:{duration}" if duration else action]
                else:
                    converted[key] = [str(value)]
            settings_value = converted

        if encrypt_current and isinstance(settings_value, str):
            settings_value = fernet.encrypt(settings_value.encode()).decode()

        settings[settings_key] = settings_value
        changed = True

    settings_json = json.dumps(settings)
    await execute_query(
        """
        INSERT INTO settings (guild_id, settings_json)
        VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE settings_json = VALUES(settings_json)
        """,
        (guild_id, settings_json),
    )
    return changed
