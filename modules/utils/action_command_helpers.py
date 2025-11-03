"""Shared helpers for action list app commands."""
from __future__ import annotations

from typing import Any, Callable, Sequence

from discord import Interaction

from modules.utils import mysql
from modules.utils.action_manager import ActionListManager
from modules.utils.interaction_responses import send_ephemeral_response
from modules.utils.localization import TranslateFn
from modules.utils.strike import validate_action

_FREE_ACTION_LIMIT = 2


async def _enforce_action_limit(
    *,
    guild_id: int,
    existing_actions: Sequence[str],
    new_action: str,
    translator: TranslateFn | None,
    manager: ActionListManager,
) -> tuple[bool, str | None, Sequence[str]]:
    """
    Ensure free-tier guilds do not configure more than the allowed number
    of automated actions. Returns (allowed, denial_message).
    """
    existing_list = list(existing_actions)
    limit = max(0, _FREE_ACTION_LIMIT)
    if len(existing_actions) < limit:
        return True, None, existing_list

    base = new_action.split(":", 1)[0].strip().lower()
    existing_bases = {
        entry.split(":", 1)[0].strip().lower() for entry in existing_list
    }
    if base in existing_bases:
        return True, None, existing_list

    try:
        accelerated = await mysql.is_accelerated(guild_id=guild_id)
    except Exception:
        accelerated = False

    if accelerated:
        return True, None, existing_actions

    trimmed: list[str] = []
    base_tracker: set[str] = set()
    for action in existing_list:
        if len(trimmed) >= limit:
            break
        action_base = action.split(":", 1)[0].strip().lower()
        if action_base in base_tracker:
            continue
        trimmed.append(action)
        base_tracker.add(action_base)

    if not trimmed and existing_list:
        trimmed = [existing_list[0]]

    updated_actions = await manager.set_actions(
        guild_id,
        trimmed,
        current_actions=existing_list,
    )
    kept_actions = list(updated_actions)
    kept_display = ", ".join(f"`{action}`" for action in kept_actions) or "`none`"
    fallback = (
        "Free servers can configure up to {limit} automated actions. "
        "Kept {kept} and removed the rest. Upgrade with {command} to configure more."
    )
    if callable(translator):
        message = translator(
            "modules.utils.action_command_helpers.action_limit.denied",
            placeholders={
                "limit": limit,
                "kept": kept_display,
                "command": "/accelerated subscribe",
            },
            fallback=fallback,
            guild_id=guild_id,
        )
    else:
        message = fallback.format(
            limit=limit,
            kept=kept_display,
            command="/accelerated subscribe",
        )
    return False, message, updated_actions


async def process_add_action(
    interaction: Interaction,
    *,
    manager: ActionListManager,
    translator: TranslateFn,
    validate_kwargs: dict[str, Any],
    defer: bool = True,
) -> None:
    """Validate and persist a moderation action, emitting translated output."""
    if defer and not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)

    action_str = await validate_action(**validate_kwargs)
    if action_str is None:
        return

    existing_actions: Sequence[str] = await manager.view_actions(interaction.guild.id)
    allowed, denial_message, normalized_actions = await _enforce_action_limit(
        guild_id=interaction.guild.id,
        existing_actions=existing_actions,
        new_action=action_str,
        translator=translator,
        manager=manager,
    )
    if not allowed:
        await send_ephemeral_response(interaction, content=denial_message)
        return

    message = await manager.add_action(
        interaction.guild.id,
        action_str,
        translator=translator,
        existing_actions=list(normalized_actions),
    )
    await send_ephemeral_response(interaction, content=message)


async def process_remove_action(
    interaction: Interaction,
    *,
    manager: ActionListManager,
    translator: TranslateFn,
    action: str,
    defer: bool = False,
) -> None:
    """Remove an action entry with consistent messaging."""
    if defer and not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)

    message = await manager.remove_action(interaction.guild.id, action, translator=translator)
    await send_ephemeral_response(interaction, content=message)


async def process_view_actions(
    interaction: Interaction,
    *,
    manager: ActionListManager,
    when_empty: str,
    format_message: Callable[[list[str]], str],
    defer: bool = False,
) -> None:
    """Display the configured action list using a provided formatter."""
    if defer and not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)

    actions = await manager.view_actions(interaction.guild.id)
    if not actions:
        await send_ephemeral_response(interaction, content=when_empty)
        return

    await send_ephemeral_response(interaction, content=format_message(actions))
