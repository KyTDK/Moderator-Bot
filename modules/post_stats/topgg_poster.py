import aiohttp
from discord.ext import tasks
from discord.ext.commands import AutoShardedBot
import os
from dotenv import load_dotenv

load_dotenv()
TOPGG_API_TOKEN = os.getenv('TOPGG_API_TOKEN')

@tasks.loop(minutes=30)
async def post_guild_count(bot: AutoShardedBot):
    async with aiohttp.ClientSession() as session:
        headers = {
            "Authorization": TOPGG_API_TOKEN,
            "Content-Type": "application/json"
        }

        payload = {"server_count": len(bot.guilds)}

        url = f"https://top.gg/api/bots/{bot.user.id}/stats"

        async with session.post(url, json=payload, headers=headers) as resp:
            if not resp.status == 200:
                error = await resp.text()
                print(f"[top.gg] Failed to post server count: {resp.status} | {error}")


def start_topgg_poster(bot: AutoShardedBot):
    async def starter():
        await bot.wait_until_ready()
        if not post_guild_count.is_running():
            post_guild_count.start(bot)

    bot.loop.create_task(starter())
