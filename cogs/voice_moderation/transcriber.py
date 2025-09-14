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

_WHISPER_LOCK = asyncio.Lock()


async def _load_whisper_model(model_name: str) -> WhisperModel:
    """Async-safe lazy loader for faster-whisper models with robust compute_type + device fallback (prefers CUDA)."""
    global _WHISPER_MODEL, _WHISPER_MODEL_NAME
    async with _WHISPER_LOCK:
        if _WHISPER_MODEL is not None and _WHISPER_MODEL_NAME == model_name:
            return _WHISPER_MODEL

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
                model = await loop.run_in_executor(None, _try, "cuda", ["float16", "int8_float16", "float32"])
                print(f"[whisper] loaded model='{model_name}' device='cuda'")
            except Exception as e:
                msg = str(e)
                if "cudnn" in msg.lower() or "invalid handle" in msg.lower() or "libcudnn" in msg.lower():
                    print("[whisper] CUDA available but cuDNN not usable; falling back to CPU at load.")
                    model = await loop.run_in_executor(None, _try, "cpu", ["int8", "int16", "float32"])
                else:
                    print(f"[whisper] CUDA init failed ({e!r}); falling back to CPU at load.")
                    model = await loop.run_in_executor(None, _try, "cpu", ["int8", "int16", "float32"])
        else:
            model = await loop.run_in_executor(None, _try, "cpu", ["int8", "int16", "float32"])

        _WHISPER_MODEL = model
        _WHISPER_MODEL_NAME = model_name
        return model


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

async def _transcribe_with_model(model: WhisperModel, tmp_path: str, language: Optional[str]) -> str:
    segments, info = model.transcribe(
        tmp_path,
        language=language,
        vad_filter=False,
        condition_on_previous_text=False,
    )
    return "".join(seg.text for seg in segments).strip()

async def _transcribe_wav_bytes(
    wav_bytes: bytes,
    *,
    model_name: str,
    language: Optional[str] = None,
    fp16: Optional[bool] = None,
) -> str:
    """Run faster-whisper transcription off the event loop, with cuDNN-aware CPU fallback."""
    # Prepare temp file
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        tf.write(wav_bytes)
        tmp_path = tf.name

    loop = asyncio.get_running_loop()

    try:
        # Try with whichever device the loader gave us (likely CUDA)
        model = await _load_whisper_model(model_name)

        def _run_gpu():
            return asyncio.run_coroutine_threadsafe(
                _transcribe_with_model(model, tmp_path, language), asyncio.get_event_loop()
            )

        # We can’t call loop directly inside run_in_executor; wrap properly:
        result_text = await loop.run_in_executor(None, lambda: model.transcribe(
            tmp_path,
            language=language,
            vad_filter=False,
            condition_on_previous_text=False,
        ))
        # result_text here is (segments generator, info) if we call model.transcribe directly;
        # to keep minimal change, rebuild text:
        if isinstance(result_text, tuple):
            segments, info = result_text
            text = "".join(seg.text for seg in segments).strip()
        else:
            # Safety: if the API shape changes, just return empty
            text = ""
        return text

    except Exception as e:
        # cuDNN runtime issues at inference time → retry on CPU
        msg = str(e)
        if "cudnn" in msg.lower() or "CUDNN_STATUS_" in msg or "sublibrary" in msg.lower():
            print("[whisper] cuDNN runtime error during transcribe; retrying on CPU...")
            cpu_model = await _load_whisper_model_cpu(model_name)
            # Run CPU transcribe in executor
            def _run_cpu():
                segments, info = cpu_model.transcribe(
                    tmp_path,
                    language=language,
                    vad_filter=False,
                    condition_on_previous_text=False,
                )
                return "".join(seg.text for seg in segments).strip()

            return await loop.run_in_executor(None, _run_cpu)
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
) -> Tuple[List[Tuple[int, str]], float]:
    """
    Transcribe per-user PCM bytes using local faster-whisper with lazy model loading.

    Returns (utterances, cost_usd_estimate).
    """
    model_name = "large-v3-turbo"

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