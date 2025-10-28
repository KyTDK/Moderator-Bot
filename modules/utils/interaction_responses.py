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
    if interaction.response.is_done():
        await interaction.followup.send(content, file=file, ephemeral=True)
    else:
        await interaction.response.send_message(content, file=file, ephemeral=True)
