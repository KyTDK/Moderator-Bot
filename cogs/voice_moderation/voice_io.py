import asyncio
from datetime import timedelta, datetime, timezone
from typing import Optional, Tuple, List, Dict
import sys
import os
import logging
import time

import discord
from discord.ext import voice_recv
from modules.ai.costs import TRANSCRIPTION_PRICE_PER_MINUTE_USD
from modules.utils import mysql
from cogs.voice_moderation.buffers import PCMBufferPool, BYTES_PER_SECOND
from cogs.voice_moderation.sink import CollectingSink
from cogs.voice_moderation.transcriber import transcribe_pcm_map

# Reduce noisy warnings from decoder flushes at the end of cycles
logging.getLogger("discord.ext.voice_recv.opus").setLevel(logging.ERROR)

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


async def collect_utterances(
    *,
    guild: discord.Guild,
    channel: discord.VoiceChannel,
    voice: Optional[discord.VoiceClient],
    do_listen: bool,
    listen_delta: timedelta,
    idle_delta: timedelta,
    api_key: Optional[str],
) -> Tuple[Optional[discord.VoiceClient], List[tuple[int, str, datetime]]]:
    """
    Join/move to a voice channel, optionally record, and transcribe utterances.

    Returns (voice_client, utterances). If saver mode or no audio: waits idle_delta and returns [].
    """
    # Prereqs
    _ensure_opus_loaded()

    vc = await _ensure_connected(guild=guild, channel=channel, voice=voice)
    if not vc:
        await asyncio.sleep(5)
        return None, []

    if not hasattr(vc, "listen") or not hasattr(vc, "stop_listening"):
        print(f"[VC IO] Voice client missing listen/stop_listening; type={type(vc)}")
        return vc, []

    if not api_key:
        print("[VC IO] AUTOMOD_OPENAI_KEY missing; skipping listen")
        if not do_listen:
            await asyncio.sleep(idle_delta.total_seconds())
        return vc, []

    if not discord.opus.is_loaded():
        print("[VC IO] Opus still not loaded; skipping listen (see OPUS installation notes)")
        if not do_listen:
            await asyncio.sleep(idle_delta.total_seconds())
        return vc, []

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
            print(f"[VC IO] continuous listening started in guild {guild.id} ch {channel.id}")
        except Exception as e:
            print(f"[VC IO] continuous listen failed: {e}")
            if not do_listen:
                await asyncio.sleep(idle_delta.total_seconds())
            return vc, []

    # Harvest per-user PCM for the requested window
    window_seconds = max(1.0, float(listen_delta.total_seconds()))
    pcm_map: Dict[int, bytes] = pool.harvest(window_seconds)

    # Filter very short clips
    eligible_map: Dict[int, bytes] = {uid: b for uid, b in pcm_map.items() if len(b) >= int(BYTES_PER_SECOND * 1.0)}
    if not eligible_map:
        if not do_listen:
            await asyncio.sleep(idle_delta.total_seconds())
        return vc, []

    # Compute approximate end timestamps for each user's harvested audio using monotonic->wall clock mapping
    now_wall = datetime.now(timezone.utc)
    now_mono = time.monotonic()
    end_ts_map: Dict[int, datetime] = {}
    duration_map_s: Dict[int, float] = {}
    for uid, pcm in eligible_map.items():
        last = pool.last_write_ts(uid)
        if last is not None:
            delta_s = max(0.0, now_mono - last)
            end_ts_map[uid] = now_wall - timedelta(seconds=delta_s)
        else:
            end_ts_map[uid] = now_wall
        duration_map_s[uid] = len(pcm) / float(BYTES_PER_SECOND)

    # Budget pre-check from estimated minutes
    bytes_per_minute = BYTES_PER_SECOND * 60
    est_minutes = sum(len(b) for b in eligible_map.values()) / float(bytes_per_minute)
    est_cost = round(est_minutes * TRANSCRIPTION_PRICE_PER_MINUTE_USD, 6)
    try:
        usage = await mysql.get_vcmod_usage(guild.id)
        if (usage.get("cost_usd", 0.0) + est_cost) > usage.get("limit_usd", 2.0):
            print(f"[VC IO] Budget reached; skipping (est {est_cost:.6f}usd)")
            if not do_listen:
                await asyncio.sleep(idle_delta.total_seconds())
            return vc, []
    except Exception as e:
        print(f"[VC IO] budget check failed, proceeding cautiously: {e}")

    # Transcribe and charge only if text is produced
    utterances, actual_cost = await transcribe_pcm_map(
        guild_id=guild.id,
        api_key=api_key,
        pcm_map=eligible_map,
    )

    if utterances:
        try:
            await mysql.add_vcmod_usage(guild.id, 0, actual_cost)
        except Exception as e:
            print(f"[VC IO] failed to record transcription cost: {e}")

    if not utterances:
        print("[VC IO] No transcript text produced; skipping budget charge.")
        if not do_listen:
            await asyncio.sleep(idle_delta.total_seconds())
        return vc, []

    # Attach timestamps (end of harvested audio) to each utterance
    utterances_with_ts: List[tuple[int, str, datetime]] = []
    for uid, text in utterances:
        end_ts = end_ts_map.get(uid, now_wall)
        dur = duration_map_s.get(uid, 0.0)
        # Use midpoint timestamp of the harvested audio for better ordering accuracy
        ts = end_ts - timedelta(seconds=dur / 2.0)
        utterances_with_ts.append((uid, text, ts))

    return vc, utterances_with_ts
