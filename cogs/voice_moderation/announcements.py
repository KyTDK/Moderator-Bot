from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

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
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._voice = voice
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

    def _cache_path(self, text: str) -> Path:
        digest = hashlib.sha256(f"{self._model}:{self._voice}:{text}".encode("utf-8")).hexdigest()
        return self._cache_dir / f"{digest}.wav"

    async def _load_cached_audio(self, path: Path) -> Optional[bytes]:
        if not path.exists():
            return None
        return await asyncio.to_thread(path.read_bytes)

    async def _store_cached_audio(self, path: Path, audio: bytes) -> None:
        await asyncio.to_thread(path.write_bytes, audio)

    async def _synthesize_tts(self, text: str) -> Optional[bytes]:
        if not text or not self._api_key:
            return None

        path = self._cache_path(text)
        cached = await self._load_cached_audio(path)
        if cached:
            return cached

        async with self._lock_for(path.name):
            cached = await self._load_cached_audio(path)
            if cached:
                return cached

            payload = {
                "model": self._model,
                "voice": self._voice,
                "input": text,
                "format": "wav",
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
            except Exception as exc:
                print(f"[VCMod] TTS synthesis failed: {exc}")
                return None

            try:
                await self._store_cached_audio(path, audio_bytes)
            except Exception as exc:
                print(f"[VCMod] Failed to cache TTS audio: {exc}")

        return await self._load_cached_audio(path)

    async def _play_audio(self, voice_client: discord.VoiceClient, audio_bytes: bytes) -> bool:
        if not audio_bytes:
            return False

        stream = io.BytesIO(audio_bytes)
        try:
            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(stream, pipe=True, before_options="-f wav"),
                volume=1.0,
            )
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
        channel: discord.VoiceChannel,
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
        audio = await self._synthesize_tts(text)
        if not audio:
            print(f"[VCMod] Failed to prepare join announcement for guild {guild.id} channel {channel.id}")
            return

        state.last_announce_key = key
        played = await self._play_audio(voice_client, audio)
        if not played:
            print(f"[VCMod] Failed to play join announcement for guild {guild.id} channel {channel.id}")
