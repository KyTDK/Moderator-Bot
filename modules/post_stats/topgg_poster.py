from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable

import aiohttp

from discord.ext import tasks
from discord.ext.commands import AutoShardedBot
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)
_DEFAULT_TIMEOUT_SECONDS = 15.0


def _get_topgg_token() -> str | None:
    token = os.getenv("TOPGG_API_TOKEN")
    if token:
        token = token.strip()
    return token or None


def _timeout_seconds() -> float:
    raw = os.getenv("TOPGG_HTTP_TIMEOUT")
    if raw:
        try:
            value = float(raw)
        except ValueError:
            value = _DEFAULT_TIMEOUT_SECONDS
        else:
            value = max(5.0, value)
        return value
    return _DEFAULT_TIMEOUT_SECONDS


async def _post_guild_count_once(
    bot: AutoShardedBot,
    *,
    session_factory: Callable[..., aiohttp.ClientSession] = aiohttp.ClientSession,
) -> None:
    token = _get_topgg_token()
    if not token:
        log.debug("Skipping top.gg post; TOPGG_API_TOKEN is not configured")
        return

    user = getattr(bot, "user", None)
    bot_id = getattr(user, "id", None)
    if bot_id is None:
        log.warning("Skipping top.gg post; bot user is unavailable")
        return

    headers = {
        "Content-Type": "application/json",
        "Authorization": token,
    }
    payload = {"server_count": len(bot.guilds)}
    url = f"https://top.gg/api/bots/{bot_id}/stats"
    timeout = aiohttp.ClientTimeout(total=_timeout_seconds())

    try:
        async with session_factory(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    error = await resp.text()
                    print(f"[top.gg] Failed to post server count: {resp.status} | {error}")
    except asyncio.TimeoutError:
        log.warning("top.gg post timed out after %.0fs; will retry automatically", timeout.total or _DEFAULT_TIMEOUT_SECONDS)
    except aiohttp.ClientError as exc:
        log.warning("top.gg post failed: %s", exc)
    except Exception:
        log.exception("Unexpected error while posting top.gg stats")


@tasks.loop(minutes=30)
async def post_guild_count(bot: AutoShardedBot):
    await _post_guild_count_once(bot)


def start_topgg_poster(bot: AutoShardedBot):
    async def starter():
        await bot.wait_until_ready()
        if not _get_topgg_token():
            log.info("TOPGG_API_TOKEN missing; top.gg posting disabled")
            return
        if not post_guild_count.is_running():
            post_guild_count.start(bot)

    bot.loop.create_task(starter())
