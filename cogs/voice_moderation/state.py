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
        self.api_warning_sent: bool = False
        self.last_cycle_started: Optional[datetime] = None
        self.last_cycle_failed: bool = False
        self.consecutive_failures: int = 0

    def reset_cycle(self) -> None:
        """Clear scheduling related bookkeeping."""
        self.channel_ids.clear()
        self.index = 0
        self.next_start = datetime.now(timezone.utc)
        self.last_announce_key = None
        self.last_cycle_started = None
        self.last_cycle_failed = False
        self.consecutive_failures = 0
