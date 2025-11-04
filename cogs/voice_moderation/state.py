from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional, Tuple

import discord


class GuildVCState:
    """Mutable voice state tracker for a single guild."""

    def __init__(self) -> None:
        self.channel_ids: list[int] = []
        self.index: int = 0
        self.busy_task: Optional[asyncio.Task] = None
        self.voice: Optional[discord.VoiceClient] = None
        self.next_start: datetime = datetime.now(timezone.utc)
        self.last_announce_key: Optional[Tuple[int, int]] = None

    def reset_cycle(self) -> None:
        """Clear scheduling related bookkeeping."""
        self.channel_ids.clear()
        self.index = 0
        self.next_start = datetime.now(timezone.utc)
        self.last_announce_key = None
