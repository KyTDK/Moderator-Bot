from __future__ import annotations
import time
from array import array
from typing import Optional
from discord.ext import voice_recv
from .buffers import PCMBufferPool

class CollectingSink(voice_recv.AudioSink):
    """AudioSink that continuously collects decoded PCM per user.

    - Requests PCM (s16le, 48kHz, 2ch)
    - Attributes packets via: direct Member mapping, SSRC mapping, or speaking probe
    - Appends PCM into given PCMBufferPool
    """

    _SSRC_TTL_SECONDS = 300

    def __init__(self, pool: PCMBufferPool) -> None:
        super().__init__()
        self._pool = pool
        self._ssrc_to_uid: dict[int, int] = {}
        self._ssrc_last_seen: dict[int, float] = {}
        self._packet_counts: dict[int, int] = {}

    def wants_opus(self) -> bool:
        return False

    def _resolve_uid_from_ssrc(self, ssrc: Optional[int]) -> Optional[int]:
        if not isinstance(ssrc, int):
            return None
        now = time.monotonic()
        last = self._ssrc_last_seen.get(ssrc)
        if last is not None and now - last > self._SSRC_TTL_SECONDS:
            self._ssrc_to_uid.pop(ssrc, None)
            self._ssrc_last_seen.pop(ssrc, None)
            return None
        uid = self._ssrc_to_uid.get(ssrc)
        if uid:
            self._ssrc_last_seen[ssrc] = now
        return uid

    def write(self, user, data: voice_recv.VoiceData):
        pcm = getattr(data, "pcm", None)
        if pcm is None:
            return

        uid: Optional[int] = None

        if user is not None:
            try:
                uid = int(getattr(user, "id", user))
            except Exception:
                uid = None

        if uid is None:
            ssrc = getattr(data, "ssrc", None)
            uid = self._resolve_uid_from_ssrc(ssrc)

        if uid is None:
            vc = getattr(self, "voice_client", None)
            if vc and hasattr(vc, "get_speaking"):
                try:
                    ch = getattr(vc, "channel", None)
                    members = [m for m in (getattr(ch, "members", None) or []) if not getattr(m, "bot", False)]
                    speaking_now = [m for m in members if vc.get_speaking(m)]
                    # Only trust speaking probe when exactly one non-bot member is speaking.
                    if len(speaking_now) == 1:
                        uid = int(speaking_now[0].id)
                except Exception:
                    uid = None

        if uid is None:
            return

        # Append PCM into pool
        if isinstance(pcm, (bytes, bytearray, memoryview)):
            self._pool.append(uid, bytes(pcm))
        elif isinstance(pcm, array):
            self._pool.append(uid, pcm.tobytes())

        # Occasional visibility
        c = self._packet_counts.get(uid, 0) + 1
        if c % 1000 == 0:
            print(f"[VC IO] sink wrote {c} packets for user {uid}")
        self._packet_counts[uid] = c

    def cleanup(self) -> None:
        """ Clear internal state.
        """
        try:
            self._ssrc_to_uid.clear()
            self._ssrc_last_seen.clear()
            self._packet_counts.clear()
        except Exception:
            pass

    # Speaking events keep SSRC mapping fresh
    @voice_recv.AudioSink.listener()
    def on_voice_member_speaking_state(self, member, ssrc, state):  # type: ignore[override]
        try:
            if member is not None and isinstance(ssrc, int):
                self._ssrc_to_uid[ssrc] = int(member.id)
                self._ssrc_last_seen[ssrc] = time.monotonic()
        except Exception:
            pass
