from __future__ import annotations

from discord.ext import commands

from cogs.strikes.cog import StrikesCog


async def setup(bot: commands.Bot):
    await bot.add_cog(StrikesCog(bot))


__all__ = ["StrikesCog", "setup"]
