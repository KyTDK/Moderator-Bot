from __future__ import annotations

import discord

from .config import ALLOWED_USER_IDS

__all__ = ["is_authorized", "require_dev_access"]


def is_authorized(user_id: int) -> bool:
    return user_id in ALLOWED_USER_IDS


async def require_dev_access(
    bot,
    interaction: discord.Interaction,
    *,
    denial_key: str | None = "cogs.debug.permission_denied",
) -> bool:
    if is_authorized(interaction.user.id):
        return True

    guild_id = interaction.guild.id if interaction.guild else None
    if denial_key and hasattr(bot, "translate"):
        message = bot.translate(denial_key, guild_id=guild_id)
    else:
        message = "You do not have permission to use this command."

    await interaction.followup.send(message, ephemeral=True)
    return False
