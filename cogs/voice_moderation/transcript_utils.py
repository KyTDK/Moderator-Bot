from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional, Sequence, TYPE_CHECKING

import discord
from discord.ext import commands

from modules.utils import mod_logging
if TYPE_CHECKING:
    from discord import Member as DiscordMember
else:
    DiscordMember = Any

ParticipantResolver = Callable[[int], Awaitable[Optional[DiscordMember]]]


def _resolve_blurple_colour() -> Optional[Any]:
    """Return the blurple colour factory if available across discord variants."""
    colour_cls = getattr(discord, "Colour", None)
    if colour_cls is None:
        colour_cls = getattr(discord, "Color", None)
    if colour_cls is None:
        return None
    blurple_factory = getattr(colour_cls, "blurple", None)
    if not callable(blurple_factory):
        return None
    try:
        return blurple_factory()
    except Exception:
        return None


def _resolve_embed_timestamp() -> datetime:
    """Return a timezone-aware UTC timestamp compatible with discord embeds."""
    utils_module = getattr(discord, "utils", None)
    utcnow = getattr(utils_module, "utcnow", None) if utils_module is not None else None
    if callable(utcnow):
        try:
            ts = utcnow()
            if isinstance(ts, datetime):
                return ts
        except Exception:
            pass
    return datetime.now(timezone.utc)


@dataclass
class ParticipantInfo:
    detail: str
    mention: str
    display_name: str


class TranscriptFormatter:
    """Helper that resolves participants and formats transcript content."""

    def __init__(
        self,
        *,
        guild: discord.Guild,
        transcript_texts: dict[str, str],
        member_resolver: ParticipantResolver,
    ) -> None:
        self.guild = guild
        self.transcript_texts = transcript_texts
        self._member_resolver = member_resolver
        self._cache: dict[int, ParticipantInfo] = {}

    async def _resolve_participant(self, user_id: int) -> ParticipantInfo:
        if user_id <= 0:
            return ParticipantInfo(
                detail=self.transcript_texts["unknown_speaker"],
                mention=self.transcript_texts["unknown_prefix"],
                display_name=self.transcript_texts["unknown_speaker"],
            )
        cached = self._cache.get(user_id)
        if cached:
            return cached

        member = await self._member_resolver(user_id)
        if member is not None:
            info = ParticipantInfo(
                detail=f"{member.mention} ({member.display_name}, id = {user_id})",
                mention=member.mention,
                display_name=member.display_name,
            )
        else:
            fallback = self.transcript_texts["user_fallback"].format(id=user_id)
            info = ParticipantInfo(
                detail=fallback,
                mention=f"<@{user_id}>",
                display_name=fallback,
            )
        self._cache[user_id] = info
        return info

    async def build_transcript_block(
        self, chunk: Sequence[tuple[int, str, datetime]]
    ) -> str:
        if not chunk:
            return ""
        author_label = self.transcript_texts["author_label"]
        utterance_label = self.transcript_texts["utterance_label"]
        divider = self.transcript_texts["divider"]

        parts: list[str] = []
        for uid, text, ts in sorted(chunk, key=lambda item: item[2]):
            info = await self._resolve_participant(uid)
            parts.append(
                f"{author_label}: {info.detail}\n{utterance_label}: {text}\n{divider}"
            )
        return "\n".join(parts)

    async def build_embed_lines(
        self, utterances: Sequence[tuple[int, str, datetime]]
    ) -> list[str]:
        lines: list[str] = []
        for uid, text, ts in sorted(utterances, key=lambda item: item[2]):
            info = await self._resolve_participant(uid)
            unix = int(ts.timestamp())
            stamp = f"<t:{unix}:t>"
            lines.append(
                self.transcript_texts["line"].format(
                    timestamp=stamp,
                    prefix=info.mention,
                    name=info.display_name,
                    text=text,
                )
            )
        return lines

    @staticmethod
    def chunk_text(payload: str, limit: int = 3900) -> list[str]:
        if len(payload) <= limit:
            return [payload]
        parts: list[str] = []
        idx = 0
        length = len(payload)
        while idx < length:
            end = min(idx + limit, length)
            if end < length:
                newline = payload.rfind("\n", idx, end)
                if newline != -1 and newline > idx:
                    end = newline + 1
            parts.append(payload[idx:end])
            idx = end
        return parts


class LiveTranscriptEmitter:
    """Buffers utterances and flushes them to the transcript channel in near real time."""

    def __init__(
        self,
        *,
        formatter: TranscriptFormatter,
        bot: commands.Bot,
        channel: discord.VoiceChannel,
        transcript_channel_id: Optional[int],
        high_quality: bool,
        min_utterances: int,
        max_utterances: int,
        min_interval: float,
        max_latency: float,
    ) -> None:
        self._formatter = formatter
        self._bot = bot
        self._channel = channel
        self._transcript_channel_id = transcript_channel_id
        self._high_quality = high_quality
        self._min_utterances = max(1, min_utterances)
        self._max_utterances = max(self._min_utterances, max_utterances)
        self._min_interval = max(0.0, min_interval)
        self._max_latency = max(self._min_interval, max_latency)
        self._buffer: list[tuple[int, str, datetime]] = []
        self._lock = asyncio.Lock()
        self._last_flush = time.monotonic()
        self._flush_count = 0

    async def add_chunk(
        self, chunk: Sequence[tuple[int, str, datetime]]
    ) -> None:
        if not self._transcript_channel_id or not chunk:
            return
        await self._buffer_and_maybe_flush(chunk, force=False)

    async def flush(self, force: bool = False) -> None:
        if not self._transcript_channel_id:
            return
        await self._buffer_and_maybe_flush([], force=force)

    async def _buffer_and_maybe_flush(
        self,
        chunk: Sequence[tuple[int, str, datetime]],
        *,
        force: bool,
    ) -> None:
        async with self._lock:
            if chunk:
                self._buffer.extend(chunk)

            if not self._buffer:
                return

            now = time.monotonic()
            should_flush = force
            if not should_flush:
                buffer_len = len(self._buffer)
                if buffer_len >= self._max_utterances:
                    should_flush = True
                elif (
                    buffer_len >= self._min_utterances
                    and (now - self._last_flush) >= self._min_interval
                ):
                    should_flush = True
                elif (now - self._last_flush) >= self._max_latency:
                    should_flush = True

            if not should_flush:
                return

            flush_chunk = sorted(self._buffer, key=lambda item: item[2])
            self._buffer.clear()
            self._last_flush = now
            self._flush_count += 1

        await self._send_flush(flush_chunk)

    async def _send_flush(
        self, flush_chunk: Sequence[tuple[int, str, datetime]]
    ) -> None:
        try:
            lines = await self._formatter.build_embed_lines(flush_chunk)
        except Exception as exc:
            print(f"[VCMod] failed to build live transcript: {exc}")
            return

        if not lines:
            return

        transcript_texts = self._formatter.transcript_texts
        transcript_mode = (
            transcript_texts["footer_high"]
            if self._high_quality
            else transcript_texts["footer_normal"]
        )

        payload = "\n".join(lines)
        chunks = TranscriptFormatter.chunk_text(payload, limit=3900)
        total_parts = len(chunks)

        for idx, part in enumerate(chunks, start=1):
            if total_parts == 1:
                title_suffix = f" (live {self._flush_count})"
            else:
                title_suffix = f" (live {self._flush_count}.{idx}/{total_parts})"

            embed = discord.Embed(
                title=f"{transcript_texts['title_single']}{title_suffix}",
                description=part,
                timestamp=_resolve_embed_timestamp(),
            )
            blurple_colour = _resolve_blurple_colour()
            if blurple_colour is not None:
                embed.colour = blurple_colour
            embed.add_field(
                name=transcript_texts["field_channel"],
                value=self._channel.mention,
                inline=True,
            )
            embed.add_field(
                name=transcript_texts["field_utterances"],
                value=str(len(flush_chunk)),
                inline=True,
            )
            if hasattr(embed, "set_footer"):
                try:
                    embed.set_footer(text=transcript_mode)
                except Exception:
                    setattr(embed, "footer", transcript_mode)
            else:
                setattr(embed, "footer", transcript_mode)
            try:
                await mod_logging.log_to_channel(
                    embed, self._transcript_channel_id, self._bot
                )
            except Exception as exc:
                print(f"[VCMod] failed to post live transcript: {exc}")
                break
