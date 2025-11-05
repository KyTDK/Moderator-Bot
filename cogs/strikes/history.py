from __future__ import annotations

import io
from datetime import timezone

import discord
from discord import Color, Embed, File, Interaction, Member

from modules.utils import mysql
from modules.utils.discord_utils import safe_get_user


async def get_strikes_command(
    cog,
    interaction: Interaction,
    user: Member,
) -> None:
    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild.id
    strike_texts = cog.bot.translate("cogs.strikes.get", guild_id=guild_id)
    strikes = await mysql.get_strikes(user.id, guild_id)

    if not strikes:
        await interaction.followup.send(
            embed=Embed(
                title=strike_texts["title"].format(name=user.display_name),
                description=strike_texts["empty"],
                color=Color.red(),
            ),
            ephemeral=True,
        )
        return

    entries: list[dict[str, str]] = []
    for strike_entry in strikes:
        strike_id, reason, striked_by_id, timestamp, expires_at = strike_entry
        timestamp = timestamp.replace(tzinfo=timezone.utc)
        expires_at = expires_at.replace(tzinfo=timezone.utc) if expires_at else None

        strike_by = await safe_get_user(cog.bot, striked_by_id)
        strike_by_name = strike_by.display_name if strike_by else "Unknown"
        expiry_str = f"<t:{int(expires_at.timestamp())}:R>" if expires_at else "Never"

        entries.append(
            {
                "title": f"Strike ID: {strike_id} | By: {strike_by_name}",
                "value": (
                    f"Reason: {reason}\n"
                    f"Issued: <t:{int(timestamp.timestamp())}:R>\n"
                    f"Expires: {expiry_str}"
                ),
            }
        )

    content = strike_texts["title"].format(name=user.display_name) + "\n\n"
    for entry in entries:
        content += f"{entry['title']}\n{entry['value']}\n\n"

    if len(entries) > 25 or len(content) > 6000:
        file = File(io.BytesIO(content.encode()), filename=f"{user.name}_strikes.txt")
        await interaction.followup.send(
            content=strike_texts["file_notice"],
            file=file,
            ephemeral=True,
        )
        return

    embed = Embed(
        title=strike_texts["title"].format(name=user.display_name),
        color=Color.red(),
    )
    for entry in entries:
        embed.add_field(
            name=entry["title"],
            value=entry["value"],
            inline=False,
        )
    await interaction.followup.send(embed=embed, ephemeral=True)


async def clear_strikes_command(
    cog,
    interaction: Interaction,
    user: Member,
) -> None:
    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild.id
    clear_texts = cog.bot.translate("cogs.strikes.clear", guild_id=guild_id)
    _, rows_affected = await mysql.execute_query(
        """
        DELETE FROM strikes
        WHERE user_id = %s
        AND guild_id = %s
        AND (expires_at IS NULL OR expires_at > UTC_TIMESTAMP())
        """,
        (user.id, guild_id),
    )

    if rows_affected == 0:
        await interaction.followup.send(
            clear_texts["none"].format(mention=user.mention),
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        clear_texts["success"].format(count=rows_affected, mention=user.mention),
        ephemeral=True,
    )


__all__ = ["get_strikes_command", "clear_strikes_command"]
