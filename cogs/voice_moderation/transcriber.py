from __future__ import annotations

import io
import wave
from typing import Dict, List, Tuple

from modules.ai.costs import TRANSCRIPTION_PRICE_PER_MINUTE_USD

try:
    from openai import AsyncOpenAI
except Exception:
    AsyncOpenAI = None  # type: ignore

SAMPLE_RATE = 48000
CHANNELS = 2
BYTES_PER_SAMPLE = 2
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE
BYTES_PER_MINUTE = BYTES_PER_SECOND * 60

def pcm_to_wav_bytes(pcm_bytes: bytes, *, channels: int = CHANNELS, sample_width: int = BYTES_PER_SAMPLE, sample_rate: int = SAMPLE_RATE) -> bytes:
    out = io.BytesIO()
    with wave.open(out, 'wb') as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    out.seek(0)
    return out.getvalue()

def estimate_minutes_from_pcm_map(pcm_map: Dict[int, bytes]) -> float:
    if not pcm_map:
        return 0.0
    total_bytes = sum(len(b) for b in pcm_map.values())
    return total_bytes / float(BYTES_PER_MINUTE)

async def transcribe_pcm_map(
    *,
    guild_id: int,
    api_key: str,
    pcm_map: Dict[int, bytes],
) -> Tuple[List[Tuple[int, str]], float]:
    """Transcribe per-user PCM bytes to text using transcription.

    Returns (utterances, cost_usd_estimate). Cost uses the minutes estimate of the
    input we actually send to transcription.
    """
    if AsyncOpenAI is None:
        raise RuntimeError("OpenAI Async client unavailable")

    client = AsyncOpenAI(api_key=api_key)
    utterances: List[Tuple[int, str]] = []

    # Convert and send per-user
    for uid, pcm in pcm_map.items():
        try:
            wav_bytes = pcm_to_wav_bytes(pcm)
            fobj = io.BytesIO(wav_bytes)
            fobj.name = "audio.wav"
            tr = await client.audio.transcriptions.create(model="gpt-4o-mini-transcribe", file=fobj)
            text = (getattr(tr, "text", None) or "").strip()
            if text:
                utterances.append((uid, text))
        except Exception as e:
            print(f"[VC IO] transcription failed for {uid}: {e}")

    minutes = estimate_minutes_from_pcm_map(pcm_map)
    cost_usd = round(minutes * TRANSCRIPTION_PRICE_PER_MINUTE_USD, 6)
    return utterances, cost_usd
