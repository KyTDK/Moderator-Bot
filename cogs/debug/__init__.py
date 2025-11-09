from __future__ import annotations

import discord
from discord.ext import commands

from modules.core.moderator_bot import ModeratorBot

from .cog import DebugCog, DEV_GUILD_ID

__all__ = ["setup"]


async def setup(bot: commands.Bot):
    await bot.add_cog(DebugCog(bot))
    if DEV_GUILD_ID:
        if isinstance(bot, ModeratorBot):
            await bot.ensure_command_tree_translator()
        await bot.tree.sync(guild=discord.Object(id=DEV_GUILD_ID))
