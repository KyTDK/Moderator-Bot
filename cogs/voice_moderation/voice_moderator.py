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
from modules.utils import log_channel as devlog
from .announcements import AnnouncementManager
from .backoff import VOICE_BACKOFF
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


def _cycle_timeout_seconds(listen_delta: timedelta, idle_delta: timedelta) -> float:
    """Compute an upper bound for how long a single voice cycle should run."""
    listen = max(0.0, listen_delta.total_seconds())
    idle = max(0.0, idle_delta.total_seconds())
    base = listen + idle
    margin = max(30.0, base * 0.3)
    return max(90.0, base + margin)


def _failure_delay_seconds(consecutive_failures: int, idle_seconds: float) -> float:
    """Backoff delay between cycles when we fail to connect to voice."""
    base = idle_seconds if idle_seconds > 0 else 15.0
    base = max(10.0, min(base, 45.0))
    extra = max(0, min(consecutive_failures - 1, 3)) * 0.5
    delay = base * (1.0 + extra)
    return min(90.0, delay)


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
            try:
                await asyncio.wait_for(state.busy_task, timeout=10.0)
            except asyncio.TimeoutError:
                print(f"[VCMod] voice cycle teardown timed out for guild {guild.id}; orphaning task.")
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
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

    async def _maybe_force_retry(self, guild: discord.Guild, settings: VoiceSettings, state: GuildVCState, now: datetime) -> None:
        """Force a prompt retry when joins are stalling so the bot re-enters voice quickly."""
        if not settings.enabled or not settings.channel_ids:
            return
        if state.busy_task and not state.busy_task.done():
            return

        min_failures = 1 if state.last_connected_at is None else 2
        if state.consecutive_failures < min_failures:
            return
        if now >= state.next_start:
            return

        last_attempt = state.last_connect_attempt or state.last_cycle_started or state.next_start
        stalled_for = max(0.0, (now - last_attempt).total_seconds())
        if stalled_for < 20.0:
            return

        cleared_any = False
        for cid in settings.channel_ids:
            if VOICE_BACKOFF.remaining(guild.id, cid) > 0.0:
                cleared_any = True
            VOICE_BACKOFF.clear(guild.id, cid)

        state.next_start = datetime.now(timezone.utc)

        should_log = state.last_join_alert_at is None or (now - state.last_join_alert_at).total_seconds() >= 300.0
        state.last_join_alert_at = now if should_log else state.last_join_alert_at

        if not should_log:
            return

        channels_value = ", ".join(str(cid) for cid in settings.channel_ids[:5])
        with contextlib.suppress(Exception):
            await devlog.log_to_developer_channel(
                self.bot,
                summary="Voice moderation join stalled; forcing retry",
                severity="warning",
                description=(
                    "Voice moderation could not connect to a configured channel; forcing an immediate retry "
                    "so the bot can rejoin without manual intervention."
                ),
                fields=[
                    devlog.DeveloperLogField(name="Guild ID", value=str(guild.id), inline=True),
                    devlog.DeveloperLogField(
                        name="Channels",
                        value=channels_value or "unset",
                        inline=False,
                    ),
                    devlog.DeveloperLogField(
                        name="Failures",
                        value=str(state.consecutive_failures),
                        inline=True,
                    ),
                    devlog.DeveloperLogField(
                        name="Backoff cleared",
                        value="yes" if cleared_any else "no",
                        inline=True,
                    ),
                ],
                context="voice.join",
            )

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

        timeout_seconds = _cycle_timeout_seconds(
            voice_settings.listen_delta,
            voice_settings.idle_delta,
        )
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
            started = state.last_cycle_started
            if started:
                elapsed = (now - started).total_seconds()
                if elapsed > timeout_seconds:
                    print(
                        f"[VCMod] voice cycle for guild {guild.id} exceeded {int(elapsed)}s "
                        "with no completion; resetting."
                    )
                    await self._teardown_state(guild)
                    state = self._get_state(guild.id)
                    state.last_cycle_failed = True
                    state.consecutive_failures = min(state.consecutive_failures + 1, 5)
                    delay = _failure_delay_seconds(
                        state.consecutive_failures,
                        voice_settings.idle_delta.total_seconds(),
                    )
                    state.next_start = datetime.now(timezone.utc) + timedelta(seconds=delay)
            return
        await self._maybe_force_retry(guild, voice_settings, state, now)
        if now < state.next_start:
            return

        if state.index >= len(state.channel_ids):
            state.index = 0

        channel = await self._resolve_channel(guild, voice_settings, state.index)
        if channel is None:
            state.index += 1
            state.next_start = datetime.now(timezone.utc)
            return

        failure_idle_seconds = voice_settings.idle_delta.total_seconds()

        async def _run() -> None:
            await self._run_cycle_for_channel(
                guild=guild,
                settings=voice_settings,
                channel=channel,
                transcript_only=effective_transcript_only,
            )

        state.last_cycle_started = datetime.now(timezone.utc)
        state.last_cycle_failed = False
        state.busy_task = self.bot.loop.create_task(_run())

        def _done_callback(_future: asyncio.Future) -> None:
            try:
                state.index += 1
                delay = 0.0
                if state.last_cycle_failed:
                    base_idle = failure_idle_seconds
                    if state.last_connected_at is None:
                        base_idle = min(base_idle, 12.0)
                    delay = _failure_delay_seconds(state.consecutive_failures, base_idle)
                state.next_start = datetime.now(timezone.utc) + timedelta(seconds=delay)
            except Exception:
                state.next_start = datetime.now(timezone.utc) + timedelta(seconds=10)
            finally:
                state.last_cycle_started = None

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
