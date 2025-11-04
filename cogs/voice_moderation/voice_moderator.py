import asyncio
import contextlib
import os
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
import time

import discord
from discord.ext import commands, tasks

from dotenv import load_dotenv

from modules.metrics import log_media_scan
from modules.utils import mysql, mod_logging
from modules.utils.discord_utils import safe_get_member
from modules.utils.time import parse_duration

from cogs.autonomous_moderation import helpers as am_helpers
from cogs.voice_moderation.models import VoiceModerationReport
from cogs.voice_moderation.prompt import VOICE_SYSTEM_PROMPT, BASE_SYSTEM_TOKENS
from cogs.voice_moderation.transcribe_queue import TranscriptionWorkQueue
from cogs.voice_moderation.voice_io import (
    HARVEST_WINDOW_SECONDS,
    harvest_pcm_chunk,
    transcribe_harvest_chunk,
)
from modules.ai.pipeline import run_moderation_pipeline_voice

load_dotenv()
AUTOMOD_OPENAI_KEY = os.getenv("AUTOMOD_OPENAI_KEY")
AIMOD_MODEL = os.getenv("AIMOD_MODEL", "gpt-5-nano")

violation_cache: dict[int, deque[tuple[str, str]]] = defaultdict(lambda: deque(maxlen=10))

_TRANSCRIBE_WORKER_COUNT = 3
_TRANSCRIBE_QUEUE_MAX = 6

_LIVE_FLUSH_MIN_UTTERANCES = 3
_LIVE_FLUSH_MAX_UTTERANCES = 12
_LIVE_FLUSH_MIN_INTERVAL_S = 2.5
_LIVE_FLUSH_MAX_LATENCY_S = 8.0

class _GuildVCState:
    def __init__(self) -> None:
        self.channel_ids: list[int] = []
        self.index: int = 0
        self.busy_task: Optional[asyncio.Task] = None
        self.voice: Optional[discord.VoiceClient] = None
        self.next_start: datetime = datetime.now(timezone.utc)

class VoiceModeratorCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._state: dict[int, _GuildVCState] = {}
        self.loop.start()

    def cog_unload(self) -> None:
        self.loop.cancel()

    def _get_state(self, guild_id: int) -> _GuildVCState:
        st = self._state.get(guild_id)
        if not st:
            st = _GuildVCState()
            self._state[guild_id] = st
        return st

    async def _teardown_state(self, guild: discord.Guild) -> None:
        st = self._get_state(guild.id)
        if st.busy_task and not st.busy_task.done():
            st.busy_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await st.busy_task
        st.busy_task = None

        vc = guild.voice_client or st.voice
        st.voice = None

        if vc is not None:
            sink = getattr(vc, "_mod_sink", None)
            if sink is not None and hasattr(vc, "stop_listening"):
                with contextlib.suppress(Exception):
                    vc.stop_listening()
                with contextlib.suppress(Exception):
                    sink.cleanup()
            with contextlib.suppress(Exception):
                setattr(vc, "_mod_pool", None)
                setattr(vc, "_mod_sink", None)
            if vc.is_connected():
                with contextlib.suppress(Exception):
                    await vc.disconnect(force=True)

        st.channel_ids = []
        st.index = 0
        st.next_start = datetime.now(timezone.utc)

    @tasks.loop(seconds=10)
    async def loop(self):
        now = datetime.now(timezone.utc)
        for guild in list(self.bot.guilds):
            try:
                await self._tick_guild(guild, now)
            except Exception as e:
                print(f"[VCMod] tick failed for {guild.id}: {e}")

    async def _tick_guild(self, guild: discord.Guild, now: datetime):
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

        # Resolve channel IDs list
        channel_ids: list[int] = []
        for ch in (channels or []):
            try:
                # settings store IDs; ensure int
                cid = int(getattr(ch, "id", ch))
                channel_ids.append(cid)
            except Exception:
                continue

        if not channel_ids:
            await self._teardown_state(guild)
            return

        st = self._get_state(guild.id)
        st.channel_ids = channel_ids

        if st.busy_task and not st.busy_task.done():
            return  # already working on this guild

        if now < st.next_start:
            return

        # Pick next channel
        if st.index >= len(channel_ids):
            st.index = 0

        chan_id = channel_ids[st.index]
        channel = guild.get_channel(chan_id)
        if not isinstance(channel, discord.VoiceChannel):
            # Try to fetch, but avoid REST spam
            try:
                fetched = await self.bot.fetch_channel(chan_id)
                if isinstance(fetched, discord.VoiceChannel):
                    channel = fetched
                else:
                    st.index += 1
                    st.next_start = datetime.now(timezone.utc)
                    return
            except Exception:
                st.index += 1
                st.next_start = datetime.now(timezone.utc)
                return

        high_accuracy = settings.get("vcmod-high-accuracy") or False
        rules = settings.get("vcmod-rules") or ""
        action_setting = settings.get("vcmod-detection-action") or ["auto"]
        aimod_debug = settings.get("aimod-debug") or False
        log_channel = settings.get("aimod-channel") or settings.get("monitor-channel")
        transcript_channel_id = settings.get("vcmod-transcript-channel")
        transcript_only = settings.get("vcmod-transcript-only") or False
        high_quality_transcription = settings.get("vcmod-high-quality-transcription") or False

        listen_delta = parse_duration(listen_str) or timedelta(minutes=2)
        idle_delta = parse_duration(idle_str) or timedelta(seconds=30)

        do_listen = not saver_mode

        async def _run():
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
            )

        st.busy_task = self.bot.loop.create_task(_run())

        def _done_callback(_):
            try:
                st.index += 1
                st.next_start = datetime.now(timezone.utc)
            except Exception:
                st.next_start = datetime.now(timezone.utc) + timedelta(seconds=10)

        st.busy_task.add_done_callback(_done_callback)


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
    ):
        st = self._get_state(guild.id)
        utterances: list[tuple[int, str, datetime]] = []

        transcript_texts = self.bot.translate(
            "cogs.voice_moderation.transcript",
            guild_id=guild.id,
        )
        author_label = transcript_texts["author_label"]
        utterance_label = transcript_texts["utterance_label"]
        divider = transcript_texts["divider"]
        unknown_speaker = transcript_texts["unknown_speaker"]
        unknown_prefix = transcript_texts["unknown_prefix"]

        member_cache: dict[int, tuple[str, str, str]] = {}

        async def _resolve_participant(uid: int) -> tuple[str, str, str]:
            if uid <= 0:
                return unknown_speaker, unknown_prefix, unknown_speaker
            cached = member_cache.get(uid)
            if cached:
                return cached
            member = await safe_get_member(guild, uid)
            if member is not None:
                info = (
                    f"{member.mention} ({member.display_name}, id = {uid})",
                    member.mention,
                    member.display_name,
                )
            else:
                fallback = transcript_texts["user_fallback"].format(id=uid)
                info = (
                    fallback,
                    f"<@{uid}>",
                    fallback,
                )
            member_cache[uid] = info
            return info

        async def _record_voice_metrics(
            *,
            status: str,
            report_obj: VoiceModerationReport | None,
            total_tokens: int,
            request_cost: float,
            usage_snapshot: dict[str, Any],
            duration_ms: int,
            error: str | None = None,
        ) -> None:
            violations_payload: list[dict[str, Any]] = []
            if report_obj and getattr(report_obj, "violations", None):
                for violation in list(report_obj.violations)[:10]:
                    violations_payload.append(
                        {
                            "user_id": getattr(violation, "user_id", None),
                            "rule": getattr(violation, "rule", None),
                            "reason": getattr(violation, "reason", None),
                            "actions": list(getattr(violation, "actions", []) or []),
                        }
                    )

            scan_payload: dict[str, Any] = {
                "is_nsfw": bool(violations_payload),
                "reason": status,
                "violations": violations_payload,
                "violations_count": len(violations_payload),
                "total_tokens": int(total_tokens),
                "request_cost_usd": float(request_cost or 0),
                "usage_snapshot": usage_snapshot or {},
                "transcript_only": bool(transcript_only),
                "high_accuracy": bool(high_accuracy),
            }
            if error:
                scan_payload["error"] = error

            extra_context = {
                "status": status,
                "utterance_count": len(utterances),
                "listen_window_seconds": listen_delta.total_seconds(),
                "idle_window_seconds": idle_delta.total_seconds(),
                "transcript_only": bool(transcript_only),
                "high_accuracy": bool(high_accuracy),
            }

            try:
                await log_media_scan(
                    guild_id=guild.id,
                    channel_id=getattr(channel, "id", None),
                    user_id=None,
                    message_id=None,
                    content_type="voice",
                    detected_mime=None,
                    filename=None,
                    file_size=None,
                    source="voice_pipeline",
                    scan_result=scan_payload,
                    status=status,
                    scan_duration_ms=duration_ms,
                    accelerated=await mysql.is_accelerated(guild_id=guild.id),
                    reference=f"voice:{guild.id}:{getattr(channel, 'id', 'unknown')}",
                    extra_context=extra_context,
                    scanner="voice_moderation",
                )
            except Exception as metrics_exc:
                print(f"[metrics] Voice metrics logging failed for guild {guild.id}: {metrics_exc}")

        chunk_queue: asyncio.Queue[Optional[list[tuple[int, str, datetime]]]] | None = None
        pipeline_task: Optional[asyncio.Task[None]] = None
        processed_violation_keys: set[tuple[int, str, str]] = set()
        vhist_blob = (
            "Violation history:\n"
            "Not provided by policy; do not consider prior violations.\n\n"
        )

        async def _build_transcript_block(
            chunk: list[tuple[int, str, datetime]]
        ) -> str:
            if not chunk:
                return ""
            parts: list[str] = []
            for uid, text, _ts in sorted(chunk, key=lambda x: x[2]):
                author_str, _, _ = await _resolve_participant(uid)
                parts.append(
                    f"{author_label}: {author_str}\n{utterance_label}: {text}\n{divider}"
                )
            return "\n".join(parts)

        async def _build_embed_lines(
            all_utts: list[tuple[int, str, datetime]]
        ) -> list[str]:
            lines: list[str] = []
            for uid, text, ts in sorted(all_utts, key=lambda x: x[2]):
                unix = int(ts.timestamp())
                stamp = f"<t:{unix}:t>"
                _, prefix, who = await _resolve_participant(uid)
                lines.append(
                    transcript_texts["line"].format(
                        timestamp=stamp,
                        prefix=prefix,
                        name=who,
                        text=text,
                    )
                )
            return lines

        def _chunk_text(s: str, limit: int = 3900) -> list[str]:
            if len(s) <= limit:
                return [s]
            parts: list[str] = []
            i = 0
            n = len(s)
            while i < n:
                j = min(i + limit, n)
                if j < n:
                    k = s.rfind("\n", i, j)
                    if k != -1 and k > i:
                        j = k + 1
                parts.append(s[i:j])
                i = j
            return parts

        live_lock = asyncio.Lock()
        live_buffer: list[tuple[int, str, datetime]] = []
        live_last_flush = time.monotonic()
        live_flush_count = 0

        async def _emit_live_transcript(
            chunk: list[tuple[int, str, datetime]], *, force: bool = False
        ) -> None:
            if not transcript_channel_id:
                return

            nonlocal live_last_flush, live_flush_count

            async with live_lock:
                if chunk:
                    live_buffer.extend(chunk)

                if not live_buffer:
                    return

                now_mono = time.monotonic()
                should_flush = force
                if not should_flush:
                    buffer_len = len(live_buffer)
                    if buffer_len >= _LIVE_FLUSH_MAX_UTTERANCES:
                        should_flush = True
                    elif (
                        buffer_len >= _LIVE_FLUSH_MIN_UTTERANCES
                        and (now_mono - live_last_flush) >= _LIVE_FLUSH_MIN_INTERVAL_S
                    ):
                        should_flush = True
                    elif (now_mono - live_last_flush) >= _LIVE_FLUSH_MAX_LATENCY_S:
                        should_flush = True

                if not should_flush:
                    return

                flush_chunk = sorted(live_buffer, key=lambda x: x[2])
                live_buffer.clear()
                live_last_flush = now_mono
                live_flush_count += 1

            try:
                pretty_lines = await _build_embed_lines(flush_chunk)
            except Exception as exc:
                print(f"[VCMod] failed to build live transcript: {exc}")
                return

            if not pretty_lines:
                return

            transcript_mode = (
                transcript_texts["footer_high"]
                if high_quality_transcription
                else transcript_texts["footer_normal"]
            )

            payload = "\n".join(pretty_lines)
            chunks = _chunk_text(payload, limit=3900)
            total_parts = len(chunks)

            for idx, part in enumerate(chunks, start=1):
                if total_parts == 1:
                    title_suffix = f" (live {live_flush_count})"
                else:
                    title_suffix = f" (live {live_flush_count}.{idx}/{total_parts})"

                embed = discord.Embed(
                    title=f"{transcript_texts['title_single']}{title_suffix}",
                    description=part,
                    colour=discord.Colour.blurple(),
                    timestamp=discord.utils.utcnow(),
                )
                embed.add_field(
                    name=transcript_texts["field_channel"],
                    value=channel.mention,
                    inline=True,
                )
                embed.add_field(
                    name=transcript_texts["field_utterances"],
                    value=str(len(flush_chunk)),
                    inline=True,
                )
                embed.set_footer(text=transcript_mode)
                try:
                    await mod_logging.log_to_channel(
                        embed, transcript_channel_id, self.bot
                    )
                except Exception as exc:
                    print(f"[VCMod] failed to post live transcript: {exc}")
                    break

        async def _pipeline_worker() -> None:
            assert chunk_queue is not None
            while True:
                chunk = await chunk_queue.get()
                try:
                    if chunk is None:
                        break
                    if transcript_only:
                        continue
                    transcript_blob = await _build_transcript_block(chunk)
                    if not transcript_blob.strip():
                        continue
                    pipeline_started = time.perf_counter()
                    try:
                        report, total_tokens, request_cost, usage, status = (
                            await run_moderation_pipeline_voice(
                                guild_id=guild.id,
                                api_key=AUTOMOD_OPENAI_KEY,
                                system_prompt=VOICE_SYSTEM_PROMPT,
                                rules=rules,
                                transcript_only=transcript_only,
                                violation_history_blob=vhist_blob,
                                transcript=transcript_blob,
                                base_system_tokens=BASE_SYSTEM_TOKENS,
                                default_model=AIMOD_MODEL,
                                high_accuracy=high_accuracy,
                                text_format=VoiceModerationReport,
                                estimate_tokens_fn=am_helpers.estimate_tokens,
                            )
                        )
                    except Exception as e:
                        duration_ms = int(
                            max((time.perf_counter() - pipeline_started) * 1000, 0)
                        )
                        await _record_voice_metrics(
                            status="exception",
                            report_obj=None,
                            total_tokens=0,
                            request_cost=0.0,
                            usage_snapshot={},
                            duration_ms=duration_ms,
                            error=str(e),
                        )
                        continue

                    duration_ms = int(
                        max((time.perf_counter() - pipeline_started) * 1000, 0)
                    )
                    await _record_voice_metrics(
                        status=status,
                        report_obj=report,
                        total_tokens=total_tokens,
                        request_cost=request_cost,
                        usage_snapshot=usage,
                        duration_ms=duration_ms,
                    )

                    if not report or not getattr(report, "violations", None):
                        continue

                    aggregated: dict[int, dict[str, Any]] = {}
                    for v in report.violations:
                        try:
                            uid = int(getattr(v, "user_id", 0))
                        except Exception:
                            continue
                        if not uid:
                            continue
                        actions = list(getattr(v, "actions", []) or [])
                        rule = (getattr(v, "rule", "") or "").strip()
                        reason = (getattr(v, "reason", "") or "").strip()
                        if not actions or not rule:
                            continue
                        key = (uid, rule, reason)
                        if key in processed_violation_keys:
                            continue
                        processed_violation_keys.add(key)
                        agg = aggregated.setdefault(
                            uid, {"actions": set(), "reasons": [], "rules": set()}
                        )
                        agg["actions"].update(actions)
                        if reason:
                            agg["reasons"].append(reason)
                        if rule:
                            agg["rules"].add(rule)

                    for uid, data in aggregated.items():
                        member = await safe_get_member(guild, uid)
                        if not member:
                            continue
                        all_actions = list(data["actions"]) if data.get("actions") else []
                        configured = am_helpers.resolve_configured_actions(
                            {"vcmod-detection-action": action_setting},
                            all_actions,
                            "vcmod-detection-action",
                        )

                        out_reason, out_rule = am_helpers.summarize_reason_rule(
                            self.bot, guild, data.get("reasons"), data.get("rules")
                        )

                        await am_helpers.apply_actions_and_log(
                            bot=self.bot,
                            member=member,
                            configured_actions=configured,
                            reason=out_reason,
                            rule=out_rule,
                            messages=[],
                            aimod_debug=aimod_debug,
                            ai_channel_id=log_channel,
                            monitor_channel_id=None,
                            ai_actions=all_actions,
                            fanout=False,
                            violation_cache=violation_cache,
                        )
                finally:
                    assert chunk_queue is not None
                    chunk_queue.task_done()

        async def _transcribe_worker(
            payload: tuple[dict[int, bytes], dict[int, datetime], dict[int, float]]
        ) -> None:
            eligible_map, end_ts_map, duration_map_s = payload
            try:
                chunk_utts, _ = await transcribe_harvest_chunk(
                    guild_id=guild.id,
                    api_key=AUTOMOD_OPENAI_KEY or "",
                    eligible_map=eligible_map,
                    end_ts_map=end_ts_map,
                    duration_map_s=duration_map_s,
                    high_quality=high_quality_transcription,
                )
                if chunk_utts:
                    utterances.extend(chunk_utts)
                    await _emit_live_transcript(chunk_utts)
                    if chunk_queue is not None:
                        await chunk_queue.put(chunk_utts)
            except Exception as e:
                print(f"[VCMod] transcribe task failed: {e}")

        dispatcher = TranscriptionWorkQueue(
            worker_count=_TRANSCRIBE_WORKER_COUNT,
            max_queue_size=_TRANSCRIBE_QUEUE_MAX,
            worker_fn=_transcribe_worker,
        )

        actual_listen_seconds = 0.0

        try:
            if do_listen:
                chunk_queue = asyncio.Queue()
                pipeline_task = asyncio.create_task(_pipeline_worker())

                start = time.monotonic()
                deadline = start + listen_delta.total_seconds()
                while True:
                    iteration_start = time.monotonic()
                    (
                        st.voice,
                        eligible_map,
                        end_ts_map,
                        duration_map_s,
                    ) = await harvest_pcm_chunk(
                        guild=guild,
                        channel=channel,
                        voice=st.voice,
                        do_listen=True,
                        idle_delta=idle_delta,
                        window_seconds=HARVEST_WINDOW_SECONDS,
                    )
                    if eligible_map:
                        enqueue_wait = await dispatcher.submit(
                            (eligible_map, end_ts_map, duration_map_s)
                        )
                        if enqueue_wait >= 0.5:
                            print(
                                f"[VCMod] Throttled harvest by {enqueue_wait:.2f}s while waiting for transcription backlog"
                            )

                    if time.monotonic() >= deadline:
                        break

                    elapsed = time.monotonic() - iteration_start
                    sleep_for = HARVEST_WINDOW_SECONDS - elapsed
                    if sleep_for > 0:
                        await asyncio.sleep(
                            min(sleep_for, max(0.0, deadline - time.monotonic()))
                        )

                listen_end = time.monotonic()
                actual_listen_seconds = max(
                    0.0,
                    min(listen_delta.total_seconds(), listen_end - start),
                )
            else:
                await harvest_pcm_chunk(
                    guild=guild,
                    channel=channel,
                    voice=st.voice,
                    do_listen=False,
                    idle_delta=idle_delta,
                    window_seconds=HARVEST_WINDOW_SECONDS,
                )
                return
        except asyncio.CancelledError:
            raise
        finally:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await dispatcher.drain_and_close()
            if chunk_queue is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await chunk_queue.put(None)
                    await chunk_queue.join()
            if pipeline_task:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await pipeline_task

        await _emit_live_transcript([], force=True)

        if not utterances:
            return

        async def _sleep_after_cycle() -> None:
            idle_seconds = idle_delta.total_seconds()
            if (
                actual_listen_seconds > 0.0
                and actual_listen_seconds < listen_delta.total_seconds()
            ):
                idle_seconds = min(idle_seconds, max(5.0, actual_listen_seconds))
            await asyncio.sleep(idle_seconds)

        if transcript_channel_id:
            try:
                pretty_lines = await _build_embed_lines(utterances)
                if pretty_lines:
                    embed_transcript = "\n".join(pretty_lines)
                    chunks = _chunk_text(embed_transcript, limit=3900)
                    total = len(chunks)
                    transcript_mode = (
                        transcript_texts["footer_high"]
                        if high_quality_transcription
                        else transcript_texts["footer_normal"]
                    )

                    for idx, part in enumerate(chunks, start=1):
                        title = (
                            transcript_texts["title_single"]
                            if total == 1
                            else transcript_texts["title_part"].format(
                                index=idx, total=total
                            )
                        )
                        embed = discord.Embed(
                            title=title,
                            description=part,
                            colour=discord.Colour.blurple(),
                            timestamp=discord.utils.utcnow(),
                        )
                        embed.add_field(
                            name=transcript_texts["field_channel"],
                            value=channel.mention,
                            inline=True,
                        )
                        embed.add_field(
                            name=transcript_texts["field_utterances"],
                            value=str(len(utterances)),
                            inline=True,
                        )
                        embed.set_footer(text=transcript_mode)
                        await mod_logging.log_to_channel(
                            embed, transcript_channel_id, self.bot
                        )
            except Exception as e:
                print(f"[VCMod] failed to post transcript: {e}")

        await _sleep_after_cycle()


async def setup_voice_moderation(bot: commands.Bot):
    await bot.add_cog(VoiceModeratorCog(bot))
