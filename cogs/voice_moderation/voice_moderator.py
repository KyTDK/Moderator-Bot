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
from .announcements import AnnouncementManager
from .cycle import VoiceCycleConfig, run_voice_cycle
from .metrics import record_voice_metrics
from .settings import VoiceSettings
from .state import GuildVCState

load_dotenv()
PRIMARY_OPENAI_KEY = os.getenv("PRIMARY_OPENAI_KEY")
AIMOD_MODEL = os.getenv("AIMOD_MODEL", "gpt-5-nano")
VCMOD_TTS_MODEL = os.getenv("VCMOD_TTS_MODEL", "gpt-4o-mini-tts")
VCMOD_TTS_VOICE = os.getenv("VCMOD_TTS_VOICE", "alloy")
VCMOD_TTS_FORMAT = os.getenv("VCMOD_TTS_FORMAT", "mp3")

violation_cache: dict[int, deque[tuple[str, str]]] = defaultdict(lambda: deque(maxlen=10))


class VoiceModeratorCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._states: dict[int, GuildVCState] = {}
        self._announcements = AnnouncementManager(
            api_key=PRIMARY_OPENAI_KEY,
            model=VCMOD_TTS_MODEL,
            voice=VCMOD_TTS_VOICE,
            response_format=VCMOD_TTS_FORMAT,
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
        raw_settings = await mysql.get_settings(
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

        voice_settings = VoiceSettings.from_raw(raw_settings)

        if not voice_settings.enabled:
            await self._teardown_state(guild)
            return

        if not voice_settings.channel_ids:
            await self._teardown_state(guild)
            return

        state = self._get_state(guild.id)
        api_key_available = bool(PRIMARY_OPENAI_KEY)
        effective_transcript_only = voice_settings.transcript_only

        if not api_key_available and not effective_transcript_only:
            effective_transcript_only = True
            if not state.api_warning_sent:
                print(
                    "[VCMod] PRIMARY_OPENAI_KEY missing; "
                    f"guild {guild.id} will run voice moderation in transcript-only mode."
                )
                state.api_warning_sent = True
        elif api_key_available and state.api_warning_sent:
            state.api_warning_sent = False

        channels_changed = state.channel_ids != voice_settings.channel_ids

        if channels_changed:
            if state.busy_task and not state.busy_task.done():
                await self._teardown_state(guild)
                state = self._get_state(guild.id)
            else:
                state.reset_cycle()

        state.channel_ids = voice_settings.channel_ids

        if state.busy_task and not state.busy_task.done():
            return
        if now < state.next_start:
            return

        if state.index >= len(state.channel_ids):
            state.index = 0

        channel = await self._resolve_channel(guild, voice_settings, state.index)
        if channel is None:
            state.index += 1
            state.next_start = datetime.now(timezone.utc)
            return

        async def _run() -> None:
            await self._run_cycle_for_channel(
                guild=guild,
                settings=voice_settings,
                channel=channel,
                transcript_only=effective_transcript_only,
            )

        state.busy_task = self.bot.loop.create_task(_run())

        def _done_callback(_future: asyncio.Future) -> None:
            try:
                state.index += 1
                state.next_start = datetime.now(timezone.utc)
            except Exception:
                state.next_start = datetime.now(timezone.utc) + timedelta(seconds=10)

        state.busy_task.add_done_callback(_done_callback)

    async def _resolve_channel(
        self,
        guild: discord.Guild,
        settings: VoiceSettings,
        index: int,
    ) -> Optional[discord.VoiceChannel]:
        if index >= len(settings.channel_ids):
            return None
        chan_id = settings.channel_ids[index]
        channel = guild.get_channel(chan_id)
        if isinstance(channel, discord.VoiceChannel):
            return channel
        try:
            fetched = await self.bot.fetch_channel(chan_id)
        except Exception:
            return None
        return fetched if isinstance(fetched, discord.VoiceChannel) else None

    async def _run_cycle_for_channel(
        self,
        *,
        guild: discord.Guild,
        settings: VoiceSettings,
        channel: discord.VoiceChannel,
        transcript_only: bool,
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
            do_listen=not settings.saver_mode,
            listen_delta=settings.listen_delta,
            idle_delta=settings.idle_delta,
            high_accuracy=settings.high_accuracy,
            high_quality_transcription=settings.high_quality_transcription,
            rules=settings.rules,
            transcript_only=transcript_only,
            action_setting=settings.action_setting,
            aimod_debug=settings.aimod_debug,
            log_channel=settings.log_channel,
            transcript_channel_id=settings.transcript_channel_id,
            join_announcement=settings.join_announcement,
            transcript_texts=transcript_texts,
            announcement_texts=announcement_texts,
        )

        await run_voice_cycle(
            bot=self.bot,
            state=state,
            config=config,
            api_key=PRIMARY_OPENAI_KEY or "",
            aimod_model=AIMOD_MODEL,
            announcement_manager=self._announcements,
            violation_cache=violation_cache,
            record_metrics=record_voice_metrics,
        )


async def setup_voice_moderation(bot: commands.Bot):
    await bot.add_cog(VoiceModeratorCog(bot))
