import aiohttp
import logging
import os

from discord.ext import tasks
from discord.ext.commands import AutoShardedBot
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)


def _get_topgg_token() -> str | None:
    token = os.getenv("TOPGG_API_TOKEN")
    if token:
        token = token.strip()
    return token or None

@tasks.loop(minutes=30)
async def post_guild_count(bot: AutoShardedBot):
    token = _get_topgg_token()
    if not token:
        log.debug("Skipping top.gg post; TOPGG_API_TOKEN is not configured")
        return

    async with aiohttp.ClientSession() as session:
        headers = {
            "Content-Type": "application/json"
        }
        headers["Authorization"] = token

        payload = {"server_count": len(bot.guilds)}

        url = f"https://top.gg/api/bots/{bot.user.id}/stats"

        async with session.post(url, json=payload, headers=headers) as resp:
            if not resp.status == 200:
                error = await resp.text()
                print(f"[top.gg] Failed to post server count: {resp.status} | {error}")


def start_topgg_poster(bot: AutoShardedBot):
    async def starter():
        await bot.wait_until_ready()
        if not _get_topgg_token():
            log.info("TOPGG_API_TOKEN missing; top.gg posting disabled")
            return
        if not post_guild_count.is_running():
            post_guild_count.start(bot)

    bot.loop.create_task(starter())
