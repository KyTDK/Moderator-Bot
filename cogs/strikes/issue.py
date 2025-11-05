from __future__ import annotations

import discord
from discord import Interaction, Member

from modules.moderation import strike as strike_module
from modules.variables.TimeString import TimeString


async def issue_strike_command(
    cog,
    interaction: Interaction,
    user: Member,
    reason: str,
    expiry: str | None,
    skip_punishments: bool,
) -> None:
    """Execute the strike command workflow."""
    try:
        embed = await strike_module.strike(
            user=user,
            bot=cog.bot,
            reason=reason,
            interaction=interaction,
            expiry=TimeString(expiry),
            skip_punishments=skip_punishments,
        )
    except ValueError as ve:
        await interaction.response.send_message(str(ve), ephemeral=True)
        return

    if embed:
        embed.set_thumbnail(url=user.display_avatar.url)
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    strike_texts = cog.bot.translate(
        "cogs.strikes.strike",
        guild_id=interaction.guild.id,
    )
    await interaction.followup.send(
        strike_texts["error"],
        ephemeral=True,
    )


__all__ = ["issue_strike_command"]
