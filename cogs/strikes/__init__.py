from __future__ import annotations

from .cog import StrikesCog


async def setup(bot):
    await bot.add_cog(StrikesCog(bot))


__all__ = ["StrikesCog", "setup"]
