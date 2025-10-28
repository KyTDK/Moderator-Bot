"""Shared helpers for action list app commands."""
from __future__ import annotations

from typing import Any, Callable

from discord import Interaction

from modules.utils.action_manager import ActionListManager
from modules.utils.interaction_responses import send_ephemeral_response
from modules.utils.localization import TranslateFn
from modules.utils.strike import validate_action


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

    message = await manager.add_action(interaction.guild.id, action_str, translator=translator)
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
