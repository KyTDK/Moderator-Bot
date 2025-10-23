from __future__ import annotations

from discord.ext import commands

from .cog import VerificationCog, setup_verification

__all__ = ["VerificationCog", "setup_verification"]


async def setup(bot: commands.Bot) -> None:
    await setup_verification(bot)
