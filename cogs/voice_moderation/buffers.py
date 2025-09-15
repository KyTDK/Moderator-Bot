from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, Tuple, Optional
import time

SAMPLE_RATE = 48000
CHANNELS = 2
BYTES_PER_SAMPLE = 2  # s16le
FRAME_BYTES = CHANNELS * BYTES_PER_SAMPLE  # 4
BYTES_PER_SECOND = SAMPLE_RATE * FRAME_BYTES

@dataclass
class RollingPCMBuffer:
    data: bytearray = field(default_factory=bytearray)
    read_offset: int = 0       # bytes already harvested (aligned to FRAME_BYTES)
    last_write_ts: float = field(default_factory=time.monotonic)

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.data.extend(chunk)
        self.last_write_ts = time.monotonic()

    def pop_since_last(
        self,
        window_seconds: Optional[float] = None,
        keep_seconds: float = 10.0,
    ) -> bytes:
        """Return bytes since last read. If a window is given and the unread span exceeds it,
        only the last `window_seconds` are returned and earlier unread audio is dropped.
        Output is aligned to PCM frame boundaries."""
        start = self.read_offset
        end = len(self.data)
        if start >= end:
            return b""

        # Bound the start if a window is provided (drop earlier unread audio)
        if window_seconds and window_seconds > 0:
            max_bytes = int(BYTES_PER_SECOND * window_seconds)
            unread = end - start
            if unread > max_bytes:
                start = end - max_bytes

        # Align the END down to a frame boundary so (end - start) % FRAME_BYTES == 0
        end_aligned = end - ((end - start) % FRAME_BYTES)
        if end_aligned <= start:
            return b""

        out = bytes(self.data[start:end_aligned])
        self.read_offset = end_aligned  # advance to exactly what we returned

        # Trim old data to keep memory bounded
        target_keep_secs = max((window_seconds or 0) * 2, keep_seconds)
        keep_bytes = int(BYTES_PER_SECOND * target_keep_secs)

        if keep_bytes > 0 and len(self.data) > keep_bytes:
            # Don't trim past unread region
            max_trim = min(self.read_offset, len(self.data) - keep_bytes)
            if max_trim > 0:
                # Align the trim to a frame boundary, too
                trim_from = max_trim - (max_trim % FRAME_BYTES)
                if trim_from > 0:
                    del self.data[:trim_from]
                    self.read_offset -= trim_from

        return out

class PCMBufferPool:
    def __init__(self) -> None:
        self._buffers: Dict[int, RollingPCMBuffer] = {}

    def append(self, user_id: int, chunk: bytes) -> None:
        buf = self._buffers.get(user_id)
        if buf is None:
            buf = RollingPCMBuffer()
            self._buffers[user_id] = buf
        buf.append(chunk)

    def harvest(self, window_seconds: float, keep_seconds: float = 10.0) -> Dict[int, bytes]:
        return {
            uid: buf.pop_since_last(window_seconds, keep_seconds)
            for uid, buf in self._buffers.items()
        }

    def prune_idle(self, idle_seconds: float = 300.0) -> None:
        """Drop buffers with no writes for `idle_seconds` (e.g., user left VC)."""
        now = time.monotonic()
        dead = [uid for uid, b in self._buffers.items() if (now - b.last_write_ts) > idle_seconds]
        for uid in dead:
            self._buffers.pop(uid, None)

    def last_write_ts(self, user_id: int) -> Optional[float]:
        """Return the monotonic timestamp of the last write for a user, if available."""
        buf = self._buffers.get(user_id)
        return buf.last_write_ts if buf is not None else None
