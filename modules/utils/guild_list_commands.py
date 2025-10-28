"""Shared helpers for guild-specific list moderation commands."""
from __future__ import annotations

import io
from typing import Callable

import discord
from discord import Interaction

from modules.utils.guild_list_storage import (
    GuildListAddResult,
    add_value,
    clear_values,
    fetch_values,
    remove_value,
)
from modules.utils.interaction_responses import send_ephemeral_response
from modules.utils.localization import TranslateFn


async def add_guild_list_entry(
    interaction: Interaction,
    *,
    table: str,
    column: str,
    value: str,
    limit: int | None,
    translator: TranslateFn,
    value_placeholder: str,
    duplicate_key: str,
    success_key: str,
    limit_key: str | None = None,
    limit_placeholder: str = "limit",
    defer: bool = True,
) -> None:
    """Add a guild-scoped entry and emit the appropriate translated message."""
    if defer and not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)

    guild_id = interaction.guild.id
    result = await add_value(
        guild_id=guild_id,
        table=table,
        column=column,
        value=value,
        limit=limit,
    )

    placeholders = {value_placeholder: value}
    if result is GuildListAddResult.ALREADY_PRESENT:
        message = translator(
            duplicate_key,
            placeholders=placeholders,
            guild_id=guild_id,
        )
    elif result is GuildListAddResult.LIMIT_REACHED and limit_key is not None:
        message = translator(
            limit_key,
            placeholders={**placeholders, limit_placeholder: limit},
            guild_id=guild_id,
        )
    else:
        message = translator(
            success_key,
            placeholders=placeholders,
            guild_id=guild_id,
        )

    await send_ephemeral_response(interaction, content=message)


async def remove_guild_list_entry(
    interaction: Interaction,
    *,
    table: str,
    column: str,
    value: str,
    translator: TranslateFn,
    value_placeholder: str,
    missing_key: str,
    success_key: str,
    defer: bool = True,
) -> None:
    """Remove a guild list entry and respond with translated text."""
    if defer and not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)

    guild_id = interaction.guild.id
    removed = await remove_value(
        guild_id=guild_id,
        table=table,
        column=column,
        value=value,
    )

    placeholders = {value_placeholder: value}
    key = success_key if removed else missing_key
    message = translator(
        key,
        placeholders=placeholders,
        guild_id=guild_id,
    )
    await send_ephemeral_response(interaction, content=message)


async def clear_guild_list(
    interaction: Interaction,
    *,
    table: str,
    translator: TranslateFn,
    empty_key: str,
    success_key: str,
    defer: bool = True,
) -> None:
    """Clear a guild list, handling empty states consistently."""
    if defer and not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)

    guild_id = interaction.guild.id
    removed = await clear_values(
        guild_id=guild_id,
        table=table,
    )

    key = success_key if removed else empty_key
    message = translator(key, guild_id=guild_id)
    await send_ephemeral_response(interaction, content=message)


async def send_guild_list_file(
    interaction: Interaction,
    *,
    table: str,
    column: str,
    translator: TranslateFn,
    value_placeholder: str,
    empty_key: str,
    header_key: str,
    item_key: str,
    filename_factory: Callable[[int], str],
    item_transform: Callable[[str], str] | None = None,
    defer: bool = True,
) -> None:
    """Fetch a guild list and send it as an attachment with localized rows."""
    if defer and not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)

    guild_id = interaction.guild.id
    values = await fetch_values(
        guild_id=guild_id,
        table=table,
        column=column,
    )

    if not values:
        message = translator(empty_key, guild_id=guild_id)
        await send_ephemeral_response(interaction, content=message)
        return

    header = translator(header_key, guild_id=guild_id)
    lines = []
    for raw_value in values:
        value = item_transform(raw_value) if item_transform else raw_value
        lines.append(
            translator(
                item_key,
                placeholders={value_placeholder: value},
                guild_id=guild_id,
            )
        )
    buffer = io.StringIO(header + "\n" + "\n".join(lines))
    filename = filename_factory(guild_id)
    file = discord.File(buffer, filename=filename)
    await send_ephemeral_response(interaction, file=file)
