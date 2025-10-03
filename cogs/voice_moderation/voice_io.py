import asyncio
from datetime import timedelta, datetime, timezone
from typing import Optional, Tuple, List, Dict
import sys
import os
import logging
import time

import discord
from discord.ext import voice_recv
from modules.utils import mysql
from modules.ai.costs import (
    TRANSCRIPTION_PRICE_PER_MINUTE_USD,
    LOCAL_TRANSCRIPTION_PRICE_PER_MINUTE_USD,
)
from cogs.voice_moderation.buffers import PCMBufferPool, BYTES_PER_SECOND
from cogs.voice_moderation.sink import CollectingSink
from cogs.voice_moderation.transcriber import (
    transcribe_pcm_map,
    estimate_minutes_from_pcm_map,
)

# Reduce noisy warnings from decoder flushes at the end of cycles
logging.getLogger("discord.ext.voice_recv.opus").setLevel(logging.ERROR)

# Fixed harvest window for transcription chunks (seconds)
# Keeps segments reasonably sized even if a speaker talks continuously.
HARVEST_WINDOW_SECONDS: float = 20.0

def _ensure_opus_loaded() -> None:
    """Ensure opus is loaded for voice receive/PCM decode."""
    if discord.opus.is_loaded():
        return
    # 1) Default loader
    try:
        discord.opus._load_default()  # type: ignore[attr-defined]
    except Exception:
        pass
    if discord.opus.is_loaded():
        return

    # 2) Env-specified path
    env_path = os.getenv("OPUS_LIBRARY_PATH") or os.getenv("OPUS_DLL_PATH")
    if env_path:
        try:
            discord.opus.load_opus(env_path)
        except Exception:
            pass
    if discord.opus.is_loaded():
        return

    # 3) Common library names
    candidates = []
    if sys.platform.startswith("linux"):
        candidates = ["libopus.so.0", "libopus.so"]
    elif sys.platform == "darwin":
        candidates = ["libopus.0.dylib", "libopus.dylib"]
    elif sys.platform.startswith("win"):
        candidates = ["opus.dll", "libopus-0.dll", "libopus.dll"]

    for name in candidates:
        try:
            discord.opus.load_opus(name)
            if discord.opus.is_loaded():
                return
        except Exception:
            continue

    if not discord.opus.is_loaded():
        print(
            "[VC IO] Opus library not loaded. Install system opus and/or set OPUS_LIBRARY_PATH. "
            f"Platform={sys.platform}"
        )

async def _ensure_connected(
    *,
    guild: discord.Guild,
    channel: discord.VoiceChannel,
    voice: Optional[discord.VoiceClient],
) -> Optional[voice_recv.VoiceRecvClient]:
    """Ensure we’re connected to `channel` with VoiceRecvClient (self_deaf=False)."""
    try:
        current = guild.voice_client or voice
        if current and current.is_connected():
            try:
                current_ch = getattr(current, "channel", None)
                if current_ch and getattr(current_ch, "id", None) == channel.id:
                    if isinstance(current, voice_recv.VoiceRecvClient):
                        return current
                    try:
                        await current.disconnect(force=True)
                    except Exception:
                        pass
                    return await channel.connect(self_deaf=False, self_mute=True, cls=voice_recv.VoiceRecvClient)
            except Exception:
                pass

            # Move, or reconnect if needed
            try:
                await current.move_to(channel)
                if isinstance(current, voice_recv.VoiceRecvClient):
                    return current
                try:
                    await current.disconnect(force=True)
                except Exception:
                    pass
                return await channel.connect(self_deaf=False, self_mute=True, cls=voice_recv.VoiceRecvClient)
            except Exception:
                try:
                    await current.disconnect(force=True)
                except Exception:
                    pass
                return await channel.connect(self_deaf=False, self_mute=True, cls=voice_recv.VoiceRecvClient)

        # Fresh connect
        return await channel.connect(self_deaf=False, self_mute=True, cls=voice_recv.VoiceRecvClient)
    except Exception as e:
        print(f"[VC IO] failed to connect/move in guild {guild.id}, channel {channel.id}: {e}")
        return None


async def harvest_pcm_chunk(
    *,
    guild: discord.Guild,
    channel: discord.VoiceChannel,
    voice: Optional[discord.VoiceClient],
    do_listen: bool,
    idle_delta: timedelta,
    window_seconds: float = HARVEST_WINDOW_SECONDS,
) -> Tuple[Optional[discord.VoiceClient], Dict[int, bytes], Dict[int, datetime], Dict[int, float]]:
    """Ensure connected and sink present, then harvest a PCM chunk only (no transcription).

    Returns (voice_client, eligible_map, end_ts_map, duration_map_s).
    If saver mode or no audio: may sleep idle_delta and return empty maps.
    """
    # Prereqs
    _ensure_opus_loaded()

    vc = await _ensure_connected(guild=guild, channel=channel, voice=voice)
    if not vc:
        await asyncio.sleep(5)
        return None, {}, {}, {}

    if not hasattr(vc, "listen") or not hasattr(vc, "stop_listening"):
        print(f"[VC IO] Voice client missing listen/stop_listening; type={type(vc)}")
        return vc, {}, {}, {}

    if not discord.opus.is_loaded():
        print("[VC IO] Opus still not loaded; skipping listen (see OPUS installation notes)")
        if not do_listen:
            await asyncio.sleep(idle_delta.total_seconds())
        return vc, {}, {}, {}

    # Saver mode, idle wait then contain trascription
    if not do_listen:
        await asyncio.sleep(idle_delta.total_seconds())

    # Continuous listening + chunker:
    # Ensure a persistent sink/pool on the voice client
    pool: PCMBufferPool = getattr(vc, "_mod_pool", None)
    sink: CollectingSink = getattr(vc, "_mod_sink", None)
    if pool is None or sink is None:
        pool = PCMBufferPool()
        sink = CollectingSink(pool)
        try:
            vc.listen(sink)
            setattr(vc, "_mod_pool", pool)
            setattr(vc, "_mod_sink", sink)
        except Exception as e:
            print(f"[VC IO] continuous listen failed: {e}")
            if not do_listen:
                await asyncio.sleep(idle_delta.total_seconds())
            return vc, {}, {}, {}

    # Harvest per-user PCM using a fixed window to avoid unbounded growth during continuous speech
    pcm_map: Dict[int, bytes] = pool.harvest(window_seconds)

    # Transcribe whatever is available; do not drop short clips to avoid losing audio
    eligible_map: Dict[int, bytes] = {uid: b for uid, b in pcm_map.items() if len(b) > 0}
    if not eligible_map:
        if not do_listen:
            await asyncio.sleep(idle_delta.total_seconds())
        return vc, {}, {}, {}

    # Compute approximate end timestamps for each user's harvested audio using monotonic->wall clock mapping
    now_wall = datetime.now(timezone.utc)
    now_mono = time.monotonic()
    end_ts_map: Dict[int, datetime] = {}
    duration_map_s: Dict[int, float] = {}
    for uid, pcm in eligible_map.items():
        last = pool.last_write_ts(uid)
        # Adjust end time by the unread tail that remains after this chunk
        tail_after_chunk_s = pool.unread_seconds(uid)
        if last is not None:
            delta_s = max(0.0, (now_mono - last) + tail_after_chunk_s)
            end_ts_map[uid] = now_wall - timedelta(seconds=delta_s)
        else:
            end_ts_map[uid] = now_wall
        duration_map_s[uid] = len(pcm) / float(BYTES_PER_SECOND)

    # Simple debug output for harvesting
    total_bytes = sum(len(b) for b in eligible_map.values())
    # Estimate backlog seconds (unread after this chunk) across users for visibility
    try:
        backlog_secs = [
            (uid, round((len(pool._buffers.get(uid).data) - pool._buffers.get(uid).read_offset) / float(BYTES_PER_SECOND), 2))
            for uid in eligible_map.keys()
            if pool._buffers.get(uid) is not None
        ]
        max_backlog = max((s for _uid, s in backlog_secs), default=0.0)
    except Exception:
        max_backlog = 0.0
    print(
        f"[VC IO] Harvested chunk: users={len(eligible_map)} bytes={total_bytes} "
        f"window={window_seconds:.1f}s backlog_max={max_backlog:.2f}s"
    )

    return vc, eligible_map, end_ts_map, duration_map_s


async def transcribe_harvest_chunk(
    *,
    guild_id: int,
    api_key: str,
    eligible_map: Dict[int, bytes],
    end_ts_map: Dict[int, datetime],
    duration_map_s: Dict[int, float],
    high_quality: bool = False,
) -> Tuple[List[Tuple[int, str, datetime]], float]:
    """Transcribe a previously harvested PCM chunk and return utterances with absolute timestamps.

    Returns (utterances, cost_usd_charged_or_estimated).
    """
    if not eligible_map:
        return [], 0.0

    # Budget pre-check from estimated minutes
    est_minutes = estimate_minutes_from_pcm_map(eligible_map)
    price_per_minute = (
        TRANSCRIPTION_PRICE_PER_MINUTE_USD
        if high_quality
        else LOCAL_TRANSCRIPTION_PRICE_PER_MINUTE_USD
    )
    est_cost = round(est_minutes * price_per_minute, 6)
    try:
        usage = await mysql.get_vcmod_usage(guild_id)
        if (usage.get("cost_usd", 0.0) + est_cost) > usage.get("limit_usd", 2.0):
            print(f"[VC IO] Budget reached; skipping transcription (est {est_cost:.6f}usd)")
            return [], 0.0
    except Exception as e:
        print(f"[VC IO] budget check failed, proceeding cautiously: {e}")

    segs, actual_cost, used_remote = await transcribe_pcm_map(
        guild_id=guild_id,
        api_key=api_key,
        pcm_map=eligible_map,
        high_quality=high_quality,
    )

    if segs:
        try:
            await mysql.add_vcmod_usage(guild_id, 0, actual_cost)
        except Exception as e:
            print(f"[VC IO] failed to record transcription cost: {e}")

    if not segs:
        print("[VC IO] No transcript text produced in this chunk; skipping budget charge.")
        return [], 0.0

    # Attach absolute timestamps to each segment midpoint for proper interleaving ordering
    utterances_with_ts: List[tuple[int, str, datetime]] = []
    for uid, text, seg_start, seg_end in segs:
        end_ts = end_ts_map.get(uid, datetime.now(timezone.utc))
        chunk_dur = duration_map_s.get(uid, 0.0)
        chunk_start_abs = end_ts - timedelta(seconds=chunk_dur)
        seg_mid_rel = (float(seg_start) + float(seg_end)) / 2.0
        ts = chunk_start_abs + timedelta(seconds=seg_mid_rel)
        utterances_with_ts.append((uid, text, ts))

    mode = "gpt-4o-mini-transcribe" if used_remote else "local-whisper"
    print(
        f"[VC IO] Transcribed chunk utterances={len(utterances_with_ts)} cost={actual_cost:.6f}usd mode={mode}"
    )
    return utterances_with_ts, actual_cost
