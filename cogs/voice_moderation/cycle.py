from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import Any, Awaitable, Callable, Dict, Optional

import discord
from discord.ext import commands

from cogs.autonomous_moderation import helpers as am_helpers
from modules.ai.pipeline import run_moderation_pipeline_voice
from modules.utils import mod_logging
from modules.utils.discord_utils import safe_get_member

from .announcements import AnnouncementManager
from .models import VoiceModerationReport
from .prompt import BASE_SYSTEM_TOKENS, VOICE_SYSTEM_PROMPT
from .state import GuildVCState
from .transcribe_queue import TranscriptionWorkQueue
from .transcript_utils import LiveTranscriptEmitter, TranscriptFormatter
from .voice_io import HARVEST_WINDOW_SECONDS, harvest_pcm_chunk, transcribe_harvest_chunk

RecordMetricsFn = Callable[..., Awaitable[None]]

_TRANSCRIBE_WORKER_COUNT = 3
_TRANSCRIBE_QUEUE_MAX = 6
_LIVE_FLUSH_MIN_UTTERANCES = 3
_LIVE_FLUSH_MAX_UTTERANCES = 12
_LIVE_FLUSH_MIN_INTERVAL_S = 2.5
_LIVE_FLUSH_MAX_LATENCY_S = 8.0


@dataclass
class VoiceCycleConfig:
    guild: discord.Guild
    channel: discord.VoiceChannel
    do_listen: bool
    listen_delta: timedelta
    idle_delta: timedelta
    high_accuracy: bool
    high_quality_transcription: bool
    rules: str
    transcript_only: bool
    action_setting: list[str]
    aimod_debug: bool
    log_channel: Optional[int]
    transcript_channel_id: Optional[int]
    join_announcement: bool
    transcript_texts: Dict[str, Any]
    announcement_texts: Optional[Dict[str, Any]]


async def run_voice_cycle(
    *,
    bot: commands.Bot,
    state: GuildVCState,
    config: VoiceCycleConfig,
    api_key: str,
    aimod_model: str,
    announcement_manager: AnnouncementManager,
    violation_cache: Dict[int, deque[tuple[str, str]]],
    record_metrics: RecordMetricsFn,
) -> None:
    guild = config.guild
    channel = config.channel

    formatter = TranscriptFormatter(
        guild=guild,
        transcript_texts=config.transcript_texts,
        member_resolver=partial(safe_get_member, guild),
    )
    live_emitter = LiveTranscriptEmitter(
        formatter=formatter,
        bot=bot,
        channel=channel,
        transcript_channel_id=config.transcript_channel_id,
        high_quality=config.high_quality_transcription,
        min_utterances=_LIVE_FLUSH_MIN_UTTERANCES,
        max_utterances=_LIVE_FLUSH_MAX_UTTERANCES,
        min_interval=_LIVE_FLUSH_MIN_INTERVAL_S,
        max_latency=_LIVE_FLUSH_MAX_LATENCY_S,
    )

    chunk_queue: asyncio.Queue[Optional[list[tuple[int, str, datetime]]]] | None = None
    pipeline_task: Optional[asyncio.Task[None]] = None
    utterances: list[tuple[int, str, datetime]] = []
    processed_violation_keys: set[tuple[int, str, str]] = set()
    vhist_blob = (
        "Violation history:\n"
        "Not provided by policy; do not consider prior violations.\n\n"
    )

    async def _pipeline_worker() -> None:
        assert chunk_queue is not None
        while True:
            chunk = await chunk_queue.get()
            try:
                if chunk is None:
                    break
                if config.transcript_only:
                    continue
                transcript_blob = await formatter.build_transcript_block(chunk)
                if not transcript_blob.strip():
                    continue
                pipeline_started = time.perf_counter()
                try:
                    report, total_tokens, request_cost, usage, status = await run_moderation_pipeline_voice(
                        guild_id=guild.id,
                        api_key=api_key,
                        system_prompt=VOICE_SYSTEM_PROMPT,
                        rules=config.rules,
                        transcript_only=config.transcript_only,
                        violation_history_blob=vhist_blob,
                        transcript=transcript_blob,
                        base_system_tokens=BASE_SYSTEM_TOKENS,
                        default_model=aimod_model,
                        high_accuracy=config.high_accuracy,
                        text_format=VoiceModerationReport,
                        estimate_tokens_fn=am_helpers.estimate_tokens,
                    )
                except Exception as exc:
                    duration_ms = int(max((time.perf_counter() - pipeline_started) * 1000, 0))
                    await record_metrics(
                        guild=guild,
                        channel=channel,
                        transcript_only=config.transcript_only,
                        high_accuracy=config.high_accuracy,
                        listen_delta=config.listen_delta,
                        idle_delta=config.idle_delta,
                        utterance_count=len(utterances),
                        status="exception",
                        report_obj=None,
                        total_tokens=0,
                        request_cost=0.0,
                        usage_snapshot={},
                        duration_ms=duration_ms,
                        error=str(exc),
                    )
                    continue

                duration_ms = int(max((time.perf_counter() - pipeline_started) * 1000, 0))
                await record_metrics(
                    guild=guild,
                    channel=channel,
                    transcript_only=config.transcript_only,
                    high_accuracy=config.high_accuracy,
                    listen_delta=config.listen_delta,
                    idle_delta=config.idle_delta,
                    utterance_count=len(utterances),
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
                for violation in report.violations:
                    try:
                        uid = int(getattr(violation, "user_id", 0))
                    except Exception:
                        continue
                    if not uid:
                        continue
                    actions = list(getattr(violation, "actions", []) or [])
                    rule = (getattr(violation, "rule", "") or "").strip()
                    reason = (getattr(violation, "reason", "") or "").strip()
                    if not actions or not rule:
                        continue
                    key = (uid, rule, reason)
                    if key in processed_violation_keys:
                        continue
                    processed_violation_keys.add(key)
                    agg = aggregated.setdefault(uid, {"actions": set(), "reasons": [], "rules": set()})
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
                        {"vcmod-detection-action": config.action_setting},
                        all_actions,
                        "vcmod-detection-action",
                    )

                    out_reason, out_rule = am_helpers.summarize_reason_rule(
                        bot,
                        guild,
                        data.get("reasons"),
                        data.get("rules"),
                    )

                    await am_helpers.apply_actions_and_log(
                        bot=bot,
                        member=member,
                        configured_actions=configured,
                        reason=out_reason,
                        rule=out_rule,
                        messages=[],
                        aimod_debug=config.aimod_debug,
                        ai_channel_id=config.log_channel,
                        monitor_channel_id=None,
                        ai_actions=all_actions,
                        fanout=False,
                        violation_cache=violation_cache,
                    )
            finally:
                assert chunk_queue is not None
                chunk_queue.task_done()

    async def _transcribe_worker(payload: tuple[dict[int, bytes], dict[int, Any], dict[int, float]]) -> None:
        eligible_map, end_ts_map, duration_map_s = payload
        try:
            chunk_utts, _ = await transcribe_harvest_chunk(
                guild_id=guild.id,
                api_key=api_key or "",
                eligible_map=eligible_map,
                end_ts_map=end_ts_map,
                duration_map_s=duration_map_s,
                high_quality=config.high_quality_transcription,
            )
            if chunk_utts:
                utterances.extend(chunk_utts)
                await live_emitter.add_chunk(chunk_utts)
                if chunk_queue is not None:
                    await chunk_queue.put(chunk_utts)
        except Exception as exc:
            print(f"[VCMod] transcribe task failed: {exc}")

    dispatcher = TranscriptionWorkQueue(
        worker_count=_TRANSCRIBE_WORKER_COUNT,
        max_queue_size=_TRANSCRIBE_QUEUE_MAX,
        worker_fn=_transcribe_worker,
    )

    actual_listen_seconds = 0.0

    try:
        if config.do_listen:
            chunk_queue = asyncio.Queue()
            pipeline_task = asyncio.create_task(_pipeline_worker())

            start = time.monotonic()
            deadline = start + config.listen_delta.total_seconds()

            while True:
                iteration_start = time.monotonic()
                (
                    state.voice,
                    eligible_map,
                    end_ts_map,
                    duration_map_s,
                ) = await harvest_pcm_chunk(
                    guild=guild,
                    channel=channel,
                    voice=state.voice,
                    do_listen=True,
                    idle_delta=config.idle_delta,
                    window_seconds=HARVEST_WINDOW_SECONDS,
                )

                await announcement_manager.maybe_announce(
                    state=state,
                    guild=guild,
                    channel=channel,
                    transcript_only=config.transcript_only,
                    enabled=config.join_announcement,
                    texts=config.announcement_texts,
                )

                if eligible_map:
                    enqueue_wait = await dispatcher.submit((eligible_map, end_ts_map, duration_map_s))
                    if enqueue_wait >= 0.5:
                        print(
                            f"[VCMod] Throttled harvest by {enqueue_wait:.2f}s while waiting for transcription backlog"
                        )

                if time.monotonic() >= deadline:
                    break

                elapsed = time.monotonic() - iteration_start
                sleep_for = HARVEST_WINDOW_SECONDS - elapsed
                if sleep_for > 0:
                    await asyncio.sleep(min(sleep_for, max(0.0, deadline - time.monotonic())))

            listen_end = time.monotonic()
            actual_listen_seconds = max(
                0.0,
                min(config.listen_delta.total_seconds(), listen_end - start),
            )
        else:
            (
                state.voice,
                _eligible_map,
                _end_ts_map,
                _duration_map_s,
            ) = await harvest_pcm_chunk(
                guild=guild,
                channel=channel,
                voice=state.voice,
                do_listen=False,
                idle_delta=config.idle_delta,
                window_seconds=HARVEST_WINDOW_SECONDS,
            )
            await announcement_manager.maybe_announce(
                state=state,
                guild=guild,
                channel=channel,
                transcript_only=config.transcript_only,
                enabled=config.join_announcement,
                texts=config.announcement_texts,
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

    await live_emitter.flush(force=True)

    if not utterances:
        return

    async def _sleep_after_cycle() -> None:
        idle_seconds = config.idle_delta.total_seconds()
        if actual_listen_seconds > 0.0 and actual_listen_seconds < config.listen_delta.total_seconds():
            idle_seconds = min(idle_seconds, max(5.0, actual_listen_seconds))
        await asyncio.sleep(idle_seconds)

    if config.transcript_channel_id:
        try:
            pretty_lines = await formatter.build_embed_lines(utterances)
            if pretty_lines:
                embed_transcript = "\n".join(pretty_lines)
                chunks = TranscriptFormatter.chunk_text(embed_transcript, limit=3900)
                total_parts = len(chunks)
                transcript_mode = (
                    config.transcript_texts["footer_high"]
                    if config.high_quality_transcription
                    else config.transcript_texts["footer_normal"]
                )

                for idx, part in enumerate(chunks, start=1):
                    title = (
                        config.transcript_texts["title_single"]
                        if total_parts == 1
                        else config.transcript_texts["title_part"].format(index=idx, total=total_parts)
                    )
                    embed = discord.Embed(
                        title=title,
                        description=part,
                        colour=discord.Colour.blurple(),
                        timestamp=discord.utils.utcnow(),
                    )
                    embed.add_field(
                        name=config.transcript_texts["field_channel"],
                        value=channel.mention,
                        inline=True,
                    )
                    embed.add_field(
                        name=config.transcript_texts["field_utterances"],
                        value=str(len(utterances)),
                        inline=True,
                    )
                    embed.set_footer(text=transcript_mode)
                    await mod_logging.log_to_channel(embed, config.transcript_channel_id, bot)
        except Exception as exc:
            print(f"[VCMod] failed to post transcript: {exc}")

    await _sleep_after_cycle()
