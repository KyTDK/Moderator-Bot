from __future__ import annotations
import asyncio
import discord
from .state import get_pending, get_recent, trim

async def wait_for_hydration(msg: discord.Message, *, timeout: float = 4.0) -> discord.Message:
    _recent_payloads = get_recent()
    _pending = get_pending()

    if msg.attachments or msg.embeds or msg.stickers:
        return msg

    if (raw := _recent_payloads.pop(msg.id, None)) is not None:
        return discord.Message(state=msg._state, channel=msg.channel, data=raw)

    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    pending = _pending.setdefault(msg.id, [])
    pending.append(fut)

    try:
        raw = await asyncio.wait_for(fut, timeout)
    except asyncio.TimeoutError:
        pending.remove(fut)
        if not pending:
            _pending.pop(msg.id, None)
        return msg
    else:
        trim()
        return discord.Message(state=msg._state, channel=msg.channel, data=raw)
