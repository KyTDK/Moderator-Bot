from __future__ import annotations

from discord.ext import commands

from .cog import AggregatedModerationCog


async def setup(bot: commands.Bot):
    await bot.add_cog(AggregatedModerationCog(bot))


__all__ = ["AggregatedModerationCog", "setup"]
