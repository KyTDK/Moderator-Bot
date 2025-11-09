from __future__ import annotations

from typing import Optional, Tuple

from modules.utils import mysql

__all__ = ["ban_guild", "unban_guild", "refresh_guilds"]


async def ban_guild(cog, interaction, guild_id: str, reason: Optional[str]) -> None:
    try:
        target_id = int(guild_id)
        if target_id <= 0:
            raise ValueError
    except ValueError:
        await interaction.followup.send("Please provide a valid numeric guild ID.", ephemeral=True)
        return

    await mysql.ban_guild(target_id, reason)

    active_guild = cog.bot.get_guild(target_id)
    if active_guild is not None:
        async with _suppress_and_report(interaction):
            await cog.bot._handle_banned_guild(active_guild)

    await interaction.followup.send(
        f"Guild `{target_id}` has been banned." + (f" Reason stored: {reason}" if reason else ""),
        ephemeral=True,
    )


async def unban_guild(cog, interaction, guild_id: str) -> None:
    try:
        target_id = int(guild_id)
        if target_id <= 0:
            raise ValueError
    except ValueError:
        await interaction.followup.send("Please provide a valid numeric guild ID.", ephemeral=True)
        return

    removed = await mysql.unban_guild(target_id)
    if removed:
        await interaction.followup.send(f"Guild `{target_id}` has been unbanned.", ephemeral=True)
    else:
        await interaction.followup.send(f"Guild `{target_id}` was not banned.", ephemeral=True)


async def refresh_guilds(cog, interaction) -> None:
    async with _suppress_and_report(interaction):
        await cog.bot._sync_guilds_with_database()
    await interaction.followup.send("Guild synchronisation complete.", ephemeral=True)


class _suppress_and_report:
    def __init__(self, interaction):
        self.interaction = interaction

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc:
            await self.interaction.followup.send(
                f"Operation failed: {exc}",
                ephemeral=True,
            )
        return True
