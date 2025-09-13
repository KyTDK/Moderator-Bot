import asyncio
import io
import wave
from datetime import timedelta
from typing import Optional, Tuple, List, Dict
from array import array

import discord

from discord.ext import voice_recv
from modules.utils import mysql
from modules.ai.costs import WHISPER_PRICE_PER_MINUTE_USD
import sys
import os
import logging


def _ensure_opus_loaded() -> None:
    """Ensure opus is loaded for voice receive/PCM decode.

    discord.py typically loads opus automatically if present, but some
    environments require an explicit load. This mirrors the example usage.
    """
    if discord.opus.is_loaded():
        return
    # 1) Try default loader first
    try:
        discord.opus._load_default()  # type: ignore[attr-defined]
    except Exception:
        pass
    if discord.opus.is_loaded():
        return

    # 2) Try environment-provided library path
    env_path = os.getenv("OPUS_LIBRARY_PATH") or os.getenv("OPUS_DLL_PATH")
    if env_path:
        try:
            discord.opus.load_opus(env_path)
        except Exception:
            pass
    if discord.opus.is_loaded():
        return

    # 3) Try common library names based on platform
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

    # 4) Final warning if still not loaded
    if not discord.opus.is_loaded():
        print(
            "[VC IO] Opus library not loaded. Install system opus and/or set OPUS_LIBRARY_PATH. "
            f"Platform={sys.platform}"
        )


try:
    from openai import AsyncOpenAI
except Exception:  # pragma: no cover - import fallback
AsyncOpenAI = None  # type: ignore


# Reduce noisy warnings from opus decoder packet flushes (harmless at end of cycle)
logging.getLogger("discord.ext.voice_recv.opus").setLevel(logging.ERROR)


async def _ensure_connected(
    *,
    guild: discord.Guild,
    channel: discord.VoiceChannel,
    voice: Optional[discord.VoiceClient],
) -> Optional[voice_recv.VoiceRecvClient]:
    """Ensure the bot is connected to `channel` using VoiceRecvClient.

    If already connected in the same guild, reuses and moves when possible. If the existing
    client is not a VoiceRecvClient, disconnects and reconnects with the correct class.
    """
    try:
        current = guild.voice_client or voice
        # If currently connected
        if current and current.is_connected():
            # Same channel?
            try:
                current_ch = getattr(current, "channel", None)
                if current_ch and getattr(current_ch, "id", None) == channel.id:
                    if isinstance(current, voice_recv.VoiceRecvClient):
                        return current
                    # Wrong client type; reconnect with VoiceRecvClient
                    try:
                        await current.disconnect(force=True)
                    except Exception:
                        pass
                    return await channel.connect(self_deaf=False, self_mute=True, cls=voice_recv.VoiceRecvClient)
            except Exception:
                pass

            # Different channel; try moving first
            try:
                await current.move_to(channel)
                if isinstance(current, voice_recv.VoiceRecvClient):
                    return current
                # After move, still wrong type; reconnect
                try:
                    await current.disconnect(force=True)
                except Exception:
                    pass
                return await channel.connect(self_deaf=False, self_mute=True, cls=voice_recv.VoiceRecvClient)
            except Exception as move_err:
                # Could be in reconnecting state or incompatible; do a clean reconnect
                try:
                    await current.disconnect(force=True)
                except Exception:
                    pass
                try:
                    return await channel.connect(self_deaf=False, self_mute=True, cls=voice_recv.VoiceRecvClient)
                except Exception as conn_err:
                    # If "Already connected" raced, return the guild's client
                    if "Already connected" in str(conn_err):
                        gc = guild.voice_client
                        if isinstance(gc, voice_recv.VoiceRecvClient):
                            return gc
                    raise

        # Not connected: connect fresh
        return await channel.connect(self_deaf=False, self_mute=True, cls=voice_recv.VoiceRecvClient)
    except Exception as e:
        print(f"[VC IO] failed to connect/move in guild {guild.id}, channel {channel.id}: {e}")
        return None


class _CollectingSink(voice_recv.AudioSink):
    """Collects PCM audio per user into in-memory buffers.

    This sink requests decoded PCM (not opus) and aggregates bytes per user id.
    """

    def __init__(self) -> None:
        super().__init__()
        self._buffers: Dict[int, io.BytesIO] = {}
        self._counts: Dict[int, int] = {}

    def wants_opus(self) -> bool:
        return False  # request PCM

    def write(self, user, data: voice_recv.VoiceData):
        if user is None:
            return
        uid = int(getattr(user, "id", user))
        pcm = getattr(data, "pcm", None)
        if not pcm:
            return
        buf = self._buffers.get(uid)
        if buf is None:
            buf = io.BytesIO()
            self._buffers[uid] = buf
        # Ensure bytes
        if isinstance(pcm, (bytes, bytearray, memoryview)):
            buf.write(bytes(pcm))
        elif isinstance(pcm, array):
            buf.write(pcm.tobytes())
        # Occasional debug: how many packets per user
        try:
            c = self._counts.get(uid, 0) + 1
            if c % 100 == 0:
                print(f"[VC IO] sink wrote {c} packets for user {uid}")
            self._counts[uid] = c
        except Exception:
            pass

    def cleanup(self):
        # Nothing special; buffers remain accessible
        pass

    def get_user_pcm(self) -> Dict[int, bytes]:
        return {uid: b.getvalue() for uid, b in self._buffers.items() if b.getbuffer().nbytes}


async def collect_utterances(
    *,
    guild: discord.Guild,
    channel: discord.VoiceChannel,
    voice: Optional[discord.VoiceClient],
    do_listen: bool,
    listen_delta: timedelta,
    idle_delta: timedelta,
    api_key: Optional[str],
) -> Tuple[Optional[discord.VoiceClient], List[tuple[int, str]]]:
    """Join/move to a voice channel, optionally record, and transcribe utterances.

    Returns (voice_client, utterances). When no utterances are produced (including saver mode),
    the function may have already awaited for appropriate dwell time.
    """
    # Ensure opus is available before connecting
    _ensure_opus_loaded()
    # Connect/move
    voice = await _ensure_connected(guild=guild, channel=channel, voice=voice)
    if not voice:
        await asyncio.sleep(5)
        return None, []

    # Ensure we have a VoiceRecvClient with listen capability
    if not hasattr(voice, "listen") or not hasattr(voice, "stop_listening"):
        print(f"[VC IO] Voice client missing listen/stop_listening; type={type(voice)}")
        await asyncio.sleep(idle_delta.total_seconds())
        return voice, []

    # Saver mode: presence only
    if not do_listen:
        await asyncio.sleep(idle_delta.total_seconds())
        return voice, []

    # Basic prerequisites
    if AsyncOpenAI is None:
        print("[VC IO] openai client not available; skipping listen")
        await asyncio.sleep(idle_delta.total_seconds())
        return voice, []
    if not api_key:
        print("[VC IO] AUTOMOD_OPENAI_KEY missing; skipping listen")
        await asyncio.sleep(idle_delta.total_seconds())
        return voice, []
    if not discord.opus.is_loaded():
        print("[VC IO] Opus still not loaded; skipping listen (see OPUS installation notes)")
        await asyncio.sleep(idle_delta.total_seconds())
        return voice, []

    # Start listening with custom sink
    active_sink = _CollectingSink()
    listen_done = asyncio.Event()

    def _after(exc: Optional[Exception] = None):
        try:
            listen_done.set()
        except Exception:
            pass

    try:
        # Start receiving
        voice.listen(active_sink, after=_after)
        try:
            print(f"[VC IO] listening started: is_listening={getattr(voice,'is_listening',lambda:False)()} in guild {guild.id} ch {channel.id}")
        except Exception:
            pass
    except Exception as e:
        print(f"[VC IO] listen() failed: {e}")
        await asyncio.sleep(idle_delta.total_seconds())
        return voice, []

    # Wait for duration then stop
    await asyncio.sleep(listen_delta.total_seconds())
    try:
        voice.stop_listening()
    except Exception:
        pass

    try:
        await asyncio.wait_for(listen_done.wait(), timeout=5)
    except asyncio.TimeoutError:
        pass

    # Collect per-user audio bytes (PCM)
    audio_map: Dict[int, bytes] = active_sink.get_user_pcm()
    try:
        total_bytes = sum(len(b) for b in audio_map.values())
        print(f"[VC IO] captured users={len(audio_map)} total_bytes={total_bytes}")
    except Exception:
        pass

    # If nothing recorded, dwell and return
    if not audio_map:
        await asyncio.sleep(idle_delta.total_seconds())
        return voice, []

    # Estimate Whisper cost from total PCM duration
    BYTES_PER_SECOND = 48000 * 2 * 2  # sample_rate * channels * bytes_per_sample
    BYTES_PER_MINUTE = BYTES_PER_SECOND * 60
    total_minutes = sum(len(b) for b in audio_map.values()) / float(BYTES_PER_MINUTE)
    whisper_cost_usd = round(total_minutes * WHISPER_PRICE_PER_MINUTE_USD, 6)

    # Budget check for Whisper cost before calling API
    try:
        usage = await mysql.get_vcmod_usage(guild.id)
        current_cost = float(usage.get("cost_usd", 0.0))
        limit = float(usage.get("limit_usd", 2.0))
        if current_cost + whisper_cost_usd > limit:
            print(f"[VC IO] Budget reached; skipping (cost now {current_cost:.6f} + est {whisper_cost_usd:.6f} > limit {limit:.2f})")
            await asyncio.sleep(idle_delta.total_seconds())
            return voice, []
    except Exception as e:
        print(f"[VC IO] budget check failed, proceeding cautiously: {e}")

    # Transcribe with Whisper API
    client = AsyncOpenAI(api_key=api_key)
    utterances: list[tuple[int, str]] = []  # (user_id, text)
    # Convert raw PCM (s16le, 48kHz, 2ch) to WAV in-memory before sending to Whisper
    def pcm_to_wav_bytes(pcm_bytes: bytes, *, channels: int = 2, sample_width: int = 2, sample_rate: int = 48000) -> bytes:
        out = io.BytesIO()
        with wave.open(out, 'wb') as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(sample_width)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_bytes)
        out.seek(0)
        return out.getvalue()

    for uid, pcm_bytes in audio_map.items():
        try:
            # Some environments may return array('h'); normalize to bytes
            if isinstance(pcm_bytes, array):
                pcm_bytes = pcm_bytes.tobytes()
            wav_bytes = pcm_to_wav_bytes(pcm_bytes)
            fobj = io.BytesIO(wav_bytes)
            fobj.name = "audio.wav"
            tr = await client.audio.transcriptions.create(model="whisper-1", file=fobj)
            text = getattr(tr, "text", None) or ""
            if text.strip():
                utterances.append((uid, text.strip()))
        except Exception as e:
            print(f"[VC IO] transcription failed for {uid}: {e}")

    # Record Whisper usage cost (even if zero utterances)
    try:
        await mysql.add_vcmod_usage(guild.id, 0, whisper_cost_usd)
    except Exception as e:
        print(f"[VC IO] failed to record whisper cost: {e}")

    # If nothing to analyze, idle dwell similar to previous behavior
    if not utterances:
        await asyncio.sleep(idle_delta.total_seconds())
        return voice, []

    return voice, utterances
