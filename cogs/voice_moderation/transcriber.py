from __future__ import annotations

import asyncio
import io
import wave
import tempfile
import os
from typing import Dict, List, Tuple, Optional

from faster_whisper import WhisperModel

from modules.ai.costs import LOCAL_TRANSCRIPTION_PRICE_PER_MINUTE_USD

SAMPLE_RATE = 48000
CHANNELS = 2
BYTES_PER_SAMPLE = 2
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE
BYTES_PER_MINUTE = BYTES_PER_SECOND * 60

_WHISPER_MODEL: Optional[WhisperModel] = None
_WHISPER_MODEL_NAME: Optional[str] = None

_WHISPER_CPU_MODEL: Optional[WhisperModel] = None
_WHISPER_CPU_MODEL_NAME: Optional[str] = None

_WHISPER_FORCE_CPU: bool = False

_WHISPER_LOCK = asyncio.Lock()


def _normalize_text(s: str) -> str:
    # Remove newlines and collapse any repeated whitespace
    return " ".join(s.replace("\n", " ").split())


async def _load_whisper_model_cpu(model_name: str) -> WhisperModel:
    """Lazy-load and cache a CPU model (used for runtime fallback if GPU/cuDNN fails)."""
    global _WHISPER_CPU_MODEL, _WHISPER_CPU_MODEL_NAME
    async with _WHISPER_LOCK:
        if _WHISPER_CPU_MODEL is not None and _WHISPER_CPU_MODEL_NAME == model_name:
            return _WHISPER_CPU_MODEL

        loop = asyncio.get_running_loop()

        def _load():
            # Prefer int8 on CPU; fall back to float32 if needed
            try:
                return WhisperModel(model_name, device="cpu", compute_type="int8")
            except Exception:
                return WhisperModel(model_name, device="cpu", compute_type="float32")

        model = await loop.run_in_executor(None, _load)
        _WHISPER_CPU_MODEL = model
        _WHISPER_CPU_MODEL_NAME = model_name
        return model


async def _load_whisper_model(model_name: str) -> WhisperModel:
    """
    Async-safe lazy loader for faster-whisper models with robust compute_type + device fallback (prefers CUDA).
    Respects a sticky CPU fallback via _WHISPER_FORCE_CPU to avoid repeated GPU attempts.
    """
    global _WHISPER_MODEL, _WHISPER_MODEL_NAME, _WHISPER_FORCE_CPU
    async with _WHISPER_LOCK:
        # If we've forced CPU, always return the cached CPU model (or load it once)
        if _WHISPER_FORCE_CPU:
            return await _load_whisper_model_cpu(model_name)

        # If we already have a GPU model for this name, return it
        if _WHISPER_MODEL is not None and _WHISPER_MODEL_NAME == model_name:
            return _WHISPER_MODEL

        # Try CUDA first (unless FORCE_CPU is set above)
        try:
            import torch
            has_cuda = torch.cuda.is_available()
        except Exception:
            has_cuda = False

        loop = asyncio.get_running_loop()

        def _try(device: str, compute_types: list[str]) -> Optional[WhisperModel]:
            last_err = None
            for ct in compute_types:
                try:
                    return WhisperModel(model_name, device=device, compute_type=ct)
                except Exception as e:
                    last_err = e
            if last_err:
                raise last_err
            return None

        if has_cuda:
            try:
                # Prefer int8_float16 on older GPUs; fall back to float32
                model = await loop.run_in_executor(None, _try, "cuda", ["int8_float16", "float32"])
                print(f"[whisper] loaded model='{model_name}' device='cuda'")
                _WHISPER_MODEL = model
                _WHISPER_MODEL_NAME = model_name
                return model
            except Exception as e:
                msg = str(e)
                # Sticky switch to CPU if it's a cuDNN-related failure or any CUDA init error
                if "cudnn" in msg.lower() or "invalid handle" in msg.lower() or "libcudnn" in msg.lower():
                    print("[whisper] CUDA available but cuDNN not usable; switching to CPU permanently for this process.")
                else:
                    print(f"[whisper] CUDA init failed ({e!r}); switching to CPU permanently for this process.")
                _WHISPER_FORCE_CPU = True
                return await _load_whisper_model_cpu(model_name)
        else:
            _WHISPER_FORCE_CPU = True
            return await _load_whisper_model_cpu(model_name)


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
    fp16: Optional[bool] = None,  # kept for API compatibility
) -> List[Tuple[str, float, float]]:
    """
    Run faster-whisper transcription off the event loop, with cuDNN-aware CPU fallback.
    If a cuDNN error occurs, set _WHISPER_FORCE_CPU = True so we don't keep retrying CUDA.
    """
    global _WHISPER_FORCE_CPU

    # Prepare temp file
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        tf.write(wav_bytes)
        tmp_path = tf.name

    loop = asyncio.get_running_loop()

    def _gpu_run(model: WhisperModel) -> List[Tuple[str, float, float]]:
        segments, info = model.transcribe(
            tmp_path,
            language=language,
            vad_filter=False,
            condition_on_previous_text=False,
        )
        out: List[Tuple[str, float, float]] = []
        for seg in segments:
            try:
                text = _normalize_text(seg.text)
                if text:
                    # seg.start/end are seconds (float)
                    out.append((text, float(seg.start or 0.0), float(seg.end or 0.0)))
            except Exception:
                continue
        return out

    def _cpu_run(cpu_model: WhisperModel) -> List[Tuple[str, float, float]]:
        segments, info = cpu_model.transcribe(
            tmp_path,
            language=language,
            vad_filter=False,
            condition_on_previous_text=False,
        )
        out: List[Tuple[str, float, float]] = []
        for seg in segments:
            try:
                text = _normalize_text(seg.text)
                if text:
                    out.append((text, float(seg.start or 0.0), float(seg.end or 0.0)))
            except Exception:
                continue
        return out

    try:
        # If we've already forced CPU, skip GPU path entirely
        if _WHISPER_FORCE_CPU:
            cpu_model = await _load_whisper_model_cpu(model_name)
            return await loop.run_in_executor(None, _cpu_run, cpu_model)

        # Try on whatever device _load_whisper_model picks (likely CUDA the first time)
        model = await _load_whisper_model(model_name)
        # If the loader returned a CPU model due to sticky flag, skip GPU run
        if _WHISPER_FORCE_CPU:
            return await loop.run_in_executor(None, _cpu_run, model)

        # Otherwise, run on GPU
        return await loop.run_in_executor(None, _gpu_run, model)

    except Exception as e:
        # cuDNN runtime issues at inference time → switch to CPU permanently and retry once
        msg = str(e)
        if ("cudnn" in msg.lower()) or ("CUDNN_STATUS_" in msg) or ("sublibrary" in msg.lower()):
            print("[whisper] cuDNN runtime error during transcribe; switching to CPU permanently and retrying...")
            _WHISPER_FORCE_CPU = True
            cpu_model = await _load_whisper_model_cpu(model_name)
            return await loop.run_in_executor(None, _cpu_run, cpu_model)
        # Other errors propagate
        raise
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass


async def transcribe_pcm_map(
    *,
    guild_id: int,
    api_key: str,
    pcm_map: Dict[int, bytes],
    language: Optional[str] = None,
    max_concurrency: int = 2,
) -> Tuple[List[Tuple[int, str, float, float]], float]:
    """
    Transcribe per-user PCM bytes using local faster-whisper with lazy model loading.

    Returns (utterances, cost_usd_estimate).
    """
    model_name = "large-v3-turbo"

    sem = asyncio.Semaphore(max_concurrency)
    # (user_id, text, seg_start_s, seg_end_s) start/end relative to the provided PCM buffer
    utterances: List[Tuple[int, str, float, float]] = []

    async def _work(uid: int, pcm: bytes):
        if not pcm:
            return
        try:
            wav_bytes = pcm_to_wav_bytes(pcm)
            async with sem:
                segs = await _transcribe_wav_bytes(
                    wav_bytes,
                    model_name=model_name,
                    language=language,
                )
            for (text, seg_start, seg_end) in segs:
                if text:
                    utterances.append((uid, text, seg_start, seg_end))
        except Exception as e:
            print(f"[VC IO] transcription failed for {uid}: {e}")

    await asyncio.gather(*[_work(uid, pcm) for uid, pcm in pcm_map.items()])

    cost_usd = round(estimate_minutes_from_pcm_map(pcm_map) * LOCAL_TRANSCRIPTION_PRICE_PER_MINUTE_USD, 6)
    return utterances, cost_usd
