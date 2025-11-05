from __future__ import annotations

import discord
from discord import Color, Embed, Interaction, Member


async def intimidate_command(
    cog,
    interaction: Interaction,
    user: Member | None,
    channel: bool,
) -> None:
    guild_id = interaction.guild.id
    intimidate_texts = cog.bot.translate("cogs.strikes.intimidate", guild_id=guild_id)

    if user:
        embed = Embed(
            title=intimidate_texts["user_title"].format(name=user.display_name),
            description=intimidate_texts["user_body"].format(mention=user.mention),
            color=Color.red(),
        )
        if channel:
            await interaction.channel.send(embed=embed)
        else:
            await user.send(embed=embed)
    else:
        embed = Embed(
            title=intimidate_texts["guild_title"],
            description=intimidate_texts["guild_body"],
            color=Color.red(),
        )
        await interaction.channel.send(embed=embed)

    await interaction.response.send_message(intimidate_texts["confirm"], ephemeral=True)


__all__ = ["intimidate_command"]
