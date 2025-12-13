from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import discord
import httpx

from .state import GuildVCState


class AnnouncementManager:
    """Handles join announcements, including optional TTS caching."""

    _DEFAULT_ENDPOINT = "https://api.openai.com/v1/audio/speech"

    def __init__(
        self,
        *,
        api_key: Optional[str],
        model: str,
        voice: str,
        cache_dir: Optional[os.PathLike[str] | str] = None,
        timeout_seconds: float = 20.0,
        response_format: str = "mp3",
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._voice = voice
        self._response_format = (response_format or "mp3").lower()
        base_dir = Path(cache_dir) if cache_dir else Path(tempfile.gettempdir()) / "moderator_bot_voice_tts"
        base_dir.mkdir(parents=True, exist_ok=True)
        self._cache_dir = base_dir
        self._timeout = httpx.Timeout(timeout_seconds)
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, cache_key: str) -> asyncio.Lock:
        lock = self._locks.get(cache_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[cache_key] = lock
        return lock

    @staticmethod
    def _cache_key(model: str, voice: str, text: str) -> str:
        return hashlib.sha256(f"{model}:{voice}:{text}".encode("utf-8")).hexdigest()

    def _existing_cache_path(self, key: str) -> Optional[Path]:
        for candidate in self._cache_dir.glob(f"{key}.*"):
            if candidate.is_file():
                return candidate
        return None

    def _resolve_cache_path(self, key: str, fmt: Optional[str]) -> Path:
        ext = (fmt or self._response_format or "mp3").lstrip(".")
        return self._cache_dir / f"{key}.{ext}"

    @staticmethod
    def _detect_format(audio: bytes, content_type: Optional[str] = None) -> str:
        if content_type:
            lower = content_type.lower()
            if "wav" in lower:
                return "wav"
            if "mpeg" in lower or "mp3" in lower:
                return "mp3"
            if "ogg" in lower:
                return "ogg"
        if audio.startswith(b"RIFF"):
            return "wav"
        if audio.startswith(b"OggS"):
            return "ogg"
        if audio.startswith(b"ID3"):
            return "mp3"
        if len(audio) >= 2 and audio[0] == 0xFF and audio[1] in (0xF3, 0xFB, 0xF2):
            return "mp3"
        return "mp3"

    async def _load_cached_audio(self, key: str) -> Tuple[Optional[bytes], Optional[str]]:
        path = self._existing_cache_path(key)
        if not path:
            return None, None
        data = await asyncio.to_thread(path.read_bytes)
        fmt = self._detect_format(data, None)
        return data, fmt

    async def _store_cached_audio(self, key: str, audio: bytes, fmt: str) -> None:
        path = self._resolve_cache_path(key, fmt)
        await asyncio.to_thread(path.write_bytes, audio)

    async def _synthesize_tts(self, text: str) -> Tuple[Optional[bytes], Optional[str]]:
        if not text or not self._api_key:
            return None, None

        key = self._cache_key(self._model, self._voice, text)
        cached_bytes, cached_fmt = await self._load_cached_audio(key)
        if cached_bytes:
            return cached_bytes, cached_fmt

        async with self._lock_for(key):
            cached_bytes, cached_fmt = await self._load_cached_audio(key)
            if cached_bytes:
                return cached_bytes, cached_fmt

            fmt_hint = None
            payload = {
                "model": self._model,
                "voice": self._voice,
                "input": text,
                "format": self._response_format,
            }
            headers = {
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            }
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(self._DEFAULT_ENDPOINT, headers=headers, json=payload)
                    response.raise_for_status()
                    audio_bytes = response.content
                    fmt_hint = self._detect_format(audio_bytes, response.headers.get("content-type"))
            except Exception as exc:
                print(f"[VCMod] TTS synthesis failed: {exc}")
                return None, None

            fmt = fmt_hint or self._response_format or "mp3"
            try:
                await self._store_cached_audio(key, audio_bytes, fmt)
            except Exception as exc:
                print(f"[VCMod] Failed to cache TTS audio: {exc}")
            return audio_bytes, fmt

        return await self._load_cached_audio(key)

    async def _play_audio(
        self,
        voice_client: discord.VoiceClient,
        audio_bytes: bytes,
        fmt: Optional[str],
    ) -> bool:
        if not audio_bytes:
            return False

        stream = io.BytesIO(audio_bytes)
        try:
            ffmpeg_kwargs: Dict[str, Any] = {"pipe": True}
            detected_fmt = fmt or self._detect_format(audio_bytes, None)
            if detected_fmt:
                ffmpeg_kwargs["before_options"] = f"-f {detected_fmt}"
            source = discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(stream, **ffmpeg_kwargs), volume=1.0)
        except Exception as exc:
            print(f"[VCMod] Failed to create TTS audio source: {exc}")
            return False

        finished = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _on_finished(error: Optional[Exception]) -> None:
            if error:
                print(f"[VCMod] TTS playback error: {error}")
            loop.call_soon_threadsafe(finished.set)

        try:
            if voice_client.is_playing():
                voice_client.stop()
            voice_client.play(source, after=_on_finished)
        except Exception as exc:
            print(f"[VCMod] Failed to start TTS playback: {exc}")
            return False

        try:
            await asyncio.wait_for(finished.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            print("[VCMod] TTS playback timed out; stopping playback.")
            with contextlib.suppress(Exception):
                if voice_client.is_playing():
                    voice_client.stop()
            return False

        return True

    @staticmethod
    def _resolve_text(texts: Optional[Dict[str, Any]], transcript_only: bool) -> str:
        key = "transcript_only" if transcript_only else "ai_active"
        if isinstance(texts, dict):
            candidate = texts.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate

        if transcript_only:
            return (
                "Moderator Bot checking in. I'm only transcribing this channel; "
                "AI moderation actions are disabled."
            )
        return "Moderator Bot checking in. Live AI voice moderation is active in this channel."

    async def maybe_announce(
        self,
        *,
        state: GuildVCState,
        guild: discord.Guild,
        channel: discord.VoiceChannel | discord.StageChannel,
        transcript_only: bool,
        enabled: bool,
        texts: Optional[Dict[str, Any]],
    ) -> None:
        if not enabled or not self._api_key:
            return

        voice_client = state.voice
        if voice_client is None:
            return

        if not voice_client.is_connected():
            state.last_announce_key = None
            return

        key = (channel.id, id(voice_client))
        if state.last_announce_key == key:
            return

        text = self._resolve_text(texts, transcript_only)
        audio_bytes, audio_fmt = await self._synthesize_tts(text)
        if not audio_bytes:
            print(f"[VCMod] Failed to prepare join announcement for guild {guild.id} channel {channel.id}")
            return

        state.last_announce_key = key
        played = await self._play_audio(voice_client, audio_bytes, audio_fmt)
        if not played:
            print(f"[VCMod] Failed to play join announcement for guild {guild.id} channel {channel.id}")
