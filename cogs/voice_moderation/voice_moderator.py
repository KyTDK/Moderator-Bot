from __future__ import annotations

import asyncio
import contextlib
import os
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

from modules.utils import mysql
from modules.utils.time import parse_duration

from .announcements import AnnouncementManager
from .cycle import VoiceCycleConfig, run_voice_cycle
from .metrics import record_voice_metrics
from .state import GuildVCState

load_dotenv()
AUTOMOD_OPENAI_KEY = os.getenv("AUTOMOD_OPENAI_KEY")
AIMOD_MODEL = os.getenv("AIMOD_MODEL", "gpt-5-nano")
VCMOD_TTS_MODEL = os.getenv("VCMOD_TTS_MODEL", "gpt-4o-mini-tts")
VCMOD_TTS_VOICE = os.getenv("VCMOD_TTS_VOICE", "alloy")

violation_cache: dict[int, deque[tuple[str, str]]] = defaultdict(lambda: deque(maxlen=10))


class VoiceModeratorCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._states: dict[int, GuildVCState] = {}
        self._announcements = AnnouncementManager(
            api_key=AUTOMOD_OPENAI_KEY,
            model=VCMOD_TTS_MODEL,
            voice=VCMOD_TTS_VOICE,
        )
        self.loop.start()

    def cog_unload(self) -> None:
        self.loop.cancel()

    def _get_state(self, guild_id: int) -> GuildVCState:
        state = self._states.get(guild_id)
        if not state:
            state = GuildVCState()
            self._states[guild_id] = state
        return state

    async def _teardown_state(self, guild: discord.Guild) -> None:
        state = self._get_state(guild.id)
        if state.busy_task and not state.busy_task.done():
            state.busy_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await state.busy_task
        state.busy_task = None

        voice = guild.voice_client or state.voice
        state.voice = None
        state.last_announce_key = None

        if voice is not None:
            sink = getattr(voice, "_mod_sink", None)
            if sink is not None and hasattr(voice, "stop_listening"):
                with contextlib.suppress(Exception):
                    voice.stop_listening()
                with contextlib.suppress(Exception):
                    sink.cleanup()
            with contextlib.suppress(Exception):
                setattr(voice, "_mod_pool", None)
                setattr(voice, "_mod_sink", None)
            if voice.is_connected():
                with contextlib.suppress(Exception):
                    await voice.disconnect(force=True)

        state.reset_cycle()

    @tasks.loop(seconds=10)
    async def loop(self) -> None:
        now = datetime.now(timezone.utc)
        for guild in list(self.bot.guilds):
            try:
                await self._tick_guild(guild, now)
            except Exception as exc:
                print(f"[VCMod] tick failed for {guild.id}: {exc}")

    async def _tick_guild(self, guild: discord.Guild, now: datetime) -> None:
        settings = await mysql.get_settings(
            guild.id,
            [
                "vcmod-enabled",
                "vcmod-channels",
                "vcmod-listen-duration",
                "vcmod-idle-duration",
                "vcmod-saver-mode",
                "vcmod-rules",
                "vcmod-high-accuracy",
                "vcmod-high-quality-transcription",
                "vcmod-detection-action",
                "aimod-debug",
                "aimod-channel",
                "monitor-channel",
                "vcmod-transcript-channel",
                "vcmod-transcript-only",
                "vcmod-join-announcement",
            ],
        )

        enabled = settings.get("vcmod-enabled") or False
        channels = settings.get("vcmod-channels") or []
        saver_mode = settings.get("vcmod-saver-mode") or False
        listen_str = settings.get("vcmod-listen-duration") or "2m"
        idle_str = settings.get("vcmod-idle-duration") or "30s"

        if not enabled or not AUTOMOD_OPENAI_KEY:
            await self._teardown_state(guild)
            return

        channel_ids: list[int] = []
        for entry in channels or []:
            try:
                channel_ids.append(int(getattr(entry, "id", entry)))
            except Exception:
                continue

        if not channel_ids:
            await self._teardown_state(guild)
            return

        state = self._get_state(guild.id)
        state.channel_ids = channel_ids

        if state.busy_task and not state.busy_task.done():
            return
        if now < state.next_start:
            return

        if state.index >= len(channel_ids):
            state.index = 0

        chan_id = channel_ids[state.index]
        channel = guild.get_channel(chan_id)
        if not isinstance(channel, discord.VoiceChannel):
            try:
                fetched = await self.bot.fetch_channel(chan_id)
                if isinstance(fetched, discord.VoiceChannel):
                    channel = fetched
                else:
                    state.index += 1
                    state.next_start = datetime.now(timezone.utc)
                    return
            except Exception:
                state.index += 1
                state.next_start = datetime.now(timezone.utc)
                return

        high_accuracy = settings.get("vcmod-high-accuracy") or False
        rules = settings.get("vcmod-rules") or ""
        action_setting = settings.get("vcmod-detection-action") or ["auto"]
        aimod_debug = settings.get("aimod-debug") or False
        log_channel = settings.get("aimod-channel") or settings.get("monitor-channel")
        transcript_channel_id = settings.get("vcmod-transcript-channel")
        transcript_only = settings.get("vcmod-transcript-only") or False
        join_announcement = settings.get("vcmod-join-announcement") or False
        high_quality_transcription = settings.get("vcmod-high-quality-transcription") or False

        listen_delta = parse_duration(listen_str) or timedelta(minutes=2)
        idle_delta = parse_duration(idle_str) or timedelta(seconds=30)

        do_listen = not saver_mode

        async def _run() -> None:
            await self._run_cycle_for_channel(
                guild=guild,
                channel=channel,
                do_listen=do_listen,
                listen_delta=listen_delta,
                idle_delta=idle_delta,
                high_accuracy=high_accuracy,
                high_quality_transcription=high_quality_transcription,
                rules=rules,
                transcript_only=transcript_only,
                action_setting=action_setting,
                aimod_debug=aimod_debug,
                log_channel=log_channel,
                transcript_channel_id=transcript_channel_id,
                join_announcement=join_announcement,
            )

        state.busy_task = self.bot.loop.create_task(_run())

        def _done_callback(_future: asyncio.Future) -> None:
            try:
                state.index += 1
                state.next_start = datetime.now(timezone.utc)
            except Exception:
                state.next_start = datetime.now(timezone.utc) + timedelta(seconds=10)

        state.busy_task.add_done_callback(_done_callback)

    async def _run_cycle_for_channel(
        self,
        *,
        guild: discord.Guild,
        channel: discord.VoiceChannel,
        do_listen: bool,
        listen_delta: timedelta,
        idle_delta: timedelta,
        high_accuracy: bool,
        high_quality_transcription: bool,
        rules: str,
        transcript_only: bool,
        action_setting: list[str],
        aimod_debug: bool,
        log_channel: Optional[int],
        transcript_channel_id: Optional[int],
        join_announcement: bool,
    ) -> None:
        state = self._get_state(guild.id)
        transcript_texts = self.bot.translate(
            "cogs.voice_moderation.transcript",
            guild_id=guild.id,
        )
        announcement_texts = self.bot.translate(
            "cogs.voice_moderation.announce",
            guild_id=guild.id,
        )

        config = VoiceCycleConfig(
            guild=guild,
            channel=channel,
            do_listen=do_listen,
            listen_delta=listen_delta,
            idle_delta=idle_delta,
            high_accuracy=high_accuracy,
            high_quality_transcription=high_quality_transcription,
            rules=rules,
            transcript_only=transcript_only,
            action_setting=action_setting,
            aimod_debug=aimod_debug,
            log_channel=log_channel,
            transcript_channel_id=transcript_channel_id,
            join_announcement=join_announcement,
            transcript_texts=transcript_texts,
            announcement_texts=announcement_texts,
        )

        await run_voice_cycle(
            bot=self.bot,
            state=state,
            config=config,
            api_key=AUTOMOD_OPENAI_KEY or "",
            aimod_model=AIMOD_MODEL,
            announcement_manager=self._announcements,
            violation_cache=violation_cache,
            record_metrics=record_voice_metrics,
        )


async def setup_voice_moderation(bot: commands.Bot):
    await bot.add_cog(VoiceModeratorCog(bot))
