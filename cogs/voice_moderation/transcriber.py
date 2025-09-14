from __future__ import annotations

import asyncio
import io
import wave
from typing import Dict, List, Tuple, Optional

import whisper

from modules.ai.costs import LOCAL_TRANSCRIPTION_PRICE_PER_MINUTE_USD

SAMPLE_RATE = 48000
CHANNELS = 2
BYTES_PER_SAMPLE = 2
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE
BYTES_PER_MINUTE = BYTES_PER_SECOND * 60

_WHISPER_MODEL: Optional[whisper.Whisper] = None
_WHISPER_MODEL_NAME: Optional[str] = None
_WHISPER_LOCK = asyncio.Lock()


async def _load_whisper_model(model_name: str) -> whisper.Whisper:
    """Async-safe lazy loader for Whisper models."""
    global _WHISPER_MODEL, _WHISPER_MODEL_NAME
    async with _WHISPER_LOCK:
        if _WHISPER_MODEL is not None and _WHISPER_MODEL_NAME == model_name:
            return _WHISPER_MODEL

        loop = asyncio.get_running_loop()
        model = await loop.run_in_executor(None, whisper.load_model, model_name)
        _WHISPER_MODEL = model
        _WHISPER_MODEL_NAME = model_name
        return model


def pcm_to_wav_bytes(
    pcm_bytes: bytes,
    *,
    channels: int = CHANNELS,
    sample_width: int = BYTES_PER_SAMPLE,
    sample_rate: int = SAMPLE_RATE,
) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
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


async def _transcribe_wav_bytes(
    wav_bytes: bytes,
    *,
    model_name: str,
    language: Optional[str] = None,
    fp16: Optional[bool] = None, 
) -> str:
    """Run Whisper transcription off the event loop."""
    model = await _load_whisper_model(model_name)

    loop = asyncio.get_running_loop()
    def _run():
        return model.transcribe(
            io.BytesIO(wav_bytes),
            language=language,
            fp16=fp16,
        )

    result = await loop.run_in_executor(None, _run)
    return (result.get("text") or "").strip()


async def transcribe_pcm_map(
    *,
    guild_id: int,
    api_key: str,  
    pcm_map: Dict[int, bytes],
    language: Optional[str] = None,
    max_concurrency: int = 2,
) -> Tuple[List[Tuple[int, str]], float]:
    """
    Transcribe per-user PCM bytes using local Whisper with lazy model loading.

    Returns (utterances, cost_usd_estimate).
    """
    model_name = "base"

    sem = asyncio.Semaphore(max_concurrency)
    utterances: List[Tuple[int, str]] = []

    async def _work(uid: int, pcm: bytes):
        if not pcm:
            return
        try:
            wav_bytes = pcm_to_wav_bytes(pcm)
            async with sem:
                text = await _transcribe_wav_bytes(
                    wav_bytes,
                    model_name=model_name,
                    language=language,
                )
            if text:
                utterances.append((uid, text))
        except Exception as e:
            print(f"[VC IO] transcription failed for {uid}: {e}")

    await asyncio.gather(*[_work(uid, pcm) for uid, pcm in pcm_map.items()])

    cost_usd = round(estimate_minutes_from_pcm_map(pcm_map) * LOCAL_TRANSCRIPTION_PRICE_PER_MINUTE_USD, 6)

    return utterances, cost_usd