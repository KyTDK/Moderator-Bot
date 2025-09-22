from __future__ import annotations

from discord.ext import commands

from .cog import CaptchaCog

__all__ = ["CaptchaCog"]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CaptchaCog(bot))
