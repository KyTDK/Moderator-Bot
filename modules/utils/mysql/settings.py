from __future__ import annotations

import asyncio
import copy
import inspect
import json
import logging
from typing import Any, Awaitable, Callable

from modules.config.premium_plans import (
    PLAN_FREE,
    describe_plan_requirements,
)
from modules.config.settings_schema import SETTINGS_SCHEMA

from .config import fernet
from .connection import execute_query
from .premium import resolve_guild_plan

_logger = logging.getLogger(__name__)

SettingsListener = Callable[[int, str, Any], Awaitable[None] | None]

_settings_listeners: list[SettingsListener] = []


def add_settings_listener(listener: SettingsListener) -> None:
    """Register a coroutine or callback invoked when a setting changes."""

    if listener in _settings_listeners:
        return
    _settings_listeners.append(listener)


def remove_settings_listener(listener: SettingsListener) -> None:
    """Unregister a previously registered settings listener."""

    try:
        _settings_listeners.remove(listener)
    except ValueError:
        pass


def _notify_settings_listeners(guild_id: int, key: str, value: Any) -> None:
    if not _settings_listeners:
        return

    loop = asyncio.get_running_loop()

    for listener in list(_settings_listeners):
        try:
            result = listener(guild_id, key, value)
        except Exception:  # pragma: no cover - defensive logging
            _logger.exception("Settings listener %r raised an error", listener)
            continue

        if result is None:
            continue

        if inspect.isawaitable(result):
            loop.create_task(_run_listener(listener, result))


async def _run_listener(listener: SettingsListener, awaitable: Awaitable[None]) -> None:
    try:
        await awaitable
    except Exception:  # pragma: no cover - defensive logging
        _logger.exception("Settings listener %r raised an error", listener)

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

    relevant_schemas = [
        SETTINGS_SCHEMA.get(key) for key in requested if SETTINGS_SCHEMA.get(key) is not None
    ]
    requires_plan = any(getattr(schema, "required_plans", None) for schema in relevant_schemas)
    active_plan = await resolve_guild_plan(guild_id) if requires_plan else PLAN_FREE

    def process_key(key: str) -> Any:
        schema = SETTINGS_SCHEMA.get(key)
        if schema is None:
            return raw.get(key)

        default = copy.deepcopy(getattr(schema, "default", None))
        encrypted = bool(getattr(schema, "encrypted", False))
        required_plans = getattr(schema, "required_plans", None)

        value = raw.get(key, copy.deepcopy(default))

        if required_plans and active_plan not in required_plans:
            return copy.deepcopy(default)

        if encrypted and value:
            value = fernet.decrypt(value.encode()).decode()

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

    if settings_key is not None and len(requested) == 1:
        return result[requested[0]]

    return result


async def update_settings(guild_id: int, settings_key: str, settings_value):
    settings = await get_settings(guild_id)

    schema = SETTINGS_SCHEMA.get(settings_key)
    encrypt_current = schema.encrypted if schema else False

    if schema and schema.required_plans:
        active_plan = await resolve_guild_plan(guild_id)
        if active_plan not in schema.required_plans:
            requirement = describe_plan_requirements(schema.required_plans)
            raise ValueError(f"This setting requires {requirement}.")

    public_value = settings_value

    if settings_value is None:
        changed = settings.pop(settings_key, None) is not None
    else:
        processed_value = settings_value
        if settings_key == "strike-actions" and isinstance(processed_value, dict):
            converted: dict[str, list[str]] = {}
            for key, value in processed_value.items():
                if isinstance(value, list):
                    converted[key] = value
                elif isinstance(value, tuple):
                    action, duration = value
                    converted[key] = [f"{action}:{duration}" if duration else action]
                else:
                    converted[key] = [str(value)]
            processed_value = converted

        public_value = processed_value

        if encrypt_current and isinstance(processed_value, str):
            stored_value = fernet.encrypt(processed_value.encode()).decode()
        else:
            stored_value = processed_value

        settings[settings_key] = stored_value
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

    if changed:
        _notify_settings_listeners(guild_id, settings_key, public_value)

    return changed
