from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

import discord

from ..helpers.attachments import AttachmentSettingsCache


@dataclass(slots=True)
class ScanOutcome:
    text_flagged: bool = False
    media_flagged: bool = False

    def packed(self, detailed: bool):
        flagged = self.text_flagged or self.media_flagged
        if detailed:
            return {
                "flagged": flagged,
                "text_flagged": self.text_flagged,
                "media_flagged": self.media_flagged,
            }
        return flagged


@dataclass(slots=True)
class MediaScanContext:
    message: discord.Message | None
    guild_id: int | None
    nsfw_callback: Callable[..., Awaitable[None]] | None
    settings_cache: AttachmentSettingsCache
    download_cap_bytes: int | None
    author: discord.Member | None
    latency_origin: float | None

    def consume_latency_origin(self) -> float | None:
        value = self.latency_origin
        self.latency_origin = None
        return value

    def update_message(self, message: discord.Message | None) -> None:
        self.message = message
        self.author = getattr(message, "author", None)
