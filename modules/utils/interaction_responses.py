"""Utilities for sending consistent ephemeral responses."""
from __future__ import annotations

from typing import Optional

import discord
from discord import Interaction


async def send_ephemeral_response(
    interaction: Interaction,
    *,
    content: Optional[str] = None,
    file: Optional[discord.File] = None,
) -> None:
    """Send an ephemeral response, handling followups after a defer."""

    send_kwargs: dict[str, object] = {"ephemeral": True}

    if content is not None:
        send_kwargs["content"] = content

    if file is not None:
        send_kwargs["file"] = file

    if interaction.response.is_done():
        await interaction.followup.send(**send_kwargs)
    else:
        await interaction.response.send_message(**send_kwargs)
