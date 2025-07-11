from __future__ import annotations
import discord
from discord.ext import commands
from .state import get_pending, get_recent, trim

class HydrationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        msg_id = int(payload.message_id)

        _recent_payloads = get_recent()
        _recent_payloads[msg_id] = payload.data

        _pending = get_pending()
        futs = _pending.pop(msg_id, None)
        if futs:
            for fut in futs:
                if not fut.done():
                    fut.set_result(payload.data)

        trim()

async def setup(bot: commands.Bot):
    await bot.add_cog(HydrationCog(bot))