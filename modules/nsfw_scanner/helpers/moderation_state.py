from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from .latency import ModeratorLatencyTracker
from .payloads import PreparedImagePayload
from .moderation_utils import should_use_remote_source

__all__ = ["ImageModerationState"]


@dataclass
class ImageModerationState:
    payload_bytes: bytes
    payload_mime: str
    source_url: str | None
    use_remote: bool
    base64_data: str | None = None
    png_retry_attempted: bool = False
    fallback_events: list[str] = field(default_factory=list)

    @classmethod
    def from_prepared_payload(
        cls,
        prepared: PreparedImagePayload,
        *,
        latency_tracker: ModeratorLatencyTracker,
        payload_metadata: dict[str, Any] | None,
        source_url: str | None,
        allow_remote: bool,
        quality_label: str | None = None,
    ) -> "ImageModerationState":
        state = cls(
            payload_bytes=b"",
            payload_mime="",
            source_url=source_url,
            use_remote=False,
        )
        state.refresh_payload(
            prepared,
            latency_tracker=latency_tracker,
            payload_metadata=payload_metadata,
            quality_label=quality_label,
            allow_remote=allow_remote,
        )
        return state

    def refresh_payload(
        self,
        prepared: PreparedImagePayload,
        *,
        latency_tracker: ModeratorLatencyTracker,
        payload_metadata: dict[str, Any] | None,
        quality_label: str | None = None,
        allow_remote: bool | None = None,
    ) -> None:
        payload_bytes = prepared.data
        payload_mime = prepared.mime or "image/jpeg"
        payload_size = len(payload_bytes)

        latency_tracker.set_payload_detail("payload_width", prepared.width)
        latency_tracker.set_payload_detail("payload_height", prepared.height)
        latency_tracker.set_payload_detail("payload_bytes", payload_size)
        latency_tracker.set_payload_detail("payload_mime", payload_mime)
        latency_tracker.set_payload_detail("payload_resized", prepared.resized)

        quality_value = prepared.quality if prepared.quality is not None else quality_label or "n/a"
        latency_tracker.set_payload_detail("payload_quality", quality_value)

        if isinstance(payload_metadata, dict):
            payload_metadata["moderation_payload_bytes"] = payload_size
            payload_metadata["moderation_payload_mime"] = payload_mime
            payload_metadata["moderation_payload_resized"] = prepared.resized
            payload_metadata["moderation_payload_quality"] = prepared.quality

        self.payload_bytes = payload_bytes
        self.payload_mime = payload_mime
        self.base64_data = None

        if allow_remote is None:
            allow_remote_flag = self.use_remote
        else:
            allow_remote_flag = bool(allow_remote)

        strategy_label = prepared.strategy
        if self.source_url and allow_remote_flag:
            self.use_remote = should_use_remote_source(self.source_url, payload_size=payload_size)
        else:
            self.use_remote = False

        if self.use_remote and self.source_url:
            strategy_label = "remote_url"

        latency_tracker.set_payload_detail("payload_strategy", strategy_label)
        if isinstance(payload_metadata, dict):
            payload_metadata["moderation_payload_strategy"] = strategy_label

    def build_inputs(self, latency_tracker: ModeratorLatencyTracker) -> list[dict[str, Any]]:
        if self.use_remote and self.source_url:
            return [
                {
                    "type": "image_url",
                    "image_url": {"url": self.source_url},
                }
            ]

        if self.base64_data is None:
            self.base64_data = base64.b64encode(self.payload_bytes).decode()
            latency_tracker.set_payload_detail("base64_chars", len(self.base64_data))

        return [
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{self.payload_mime};base64,{self.base64_data}",
                },
            }
        ]

    def force_inline(self) -> None:
        self.use_remote = False
        self.base64_data = None

    def force_remote(self) -> None:
        if self.source_url:
            self.use_remote = True
        else:
            self.use_remote = False
        self.base64_data = None

    def mark_fallback(self, event: str) -> None:
        if event not in self.fallback_events:
            self.fallback_events.append(event)

    def fallback_message(self) -> str | None:
        if not self.fallback_events:
            return None
        readable = ", ".join(sorted(self.fallback_events))
        host = urlparse(self.source_url).netloc if self.source_url else None
        host_fragment = f" using remote host `{host}`" if host else ""
        return f"Moderator API primary payload failed; recovered via {readable}{host_fragment}."

    def logging_details(self) -> list[str]:
        details: list[str] = [f"image_remote={bool(self.use_remote)}"]
        try:
            payload_length = len(self.payload_bytes or b"")
        except TypeError:
            payload_length = 0
        if payload_length:
            details.append(f"image_payload_bytes={payload_length}")
            try:
                payload_hash = hashlib.sha256(self.payload_bytes).hexdigest()[:16]
                details.append(f"image_payload_sha256={payload_hash}")
            except Exception:
                pass
        if self.payload_mime:
            details.append(f"image_payload_mime={self.payload_mime}")
        if self.source_url:
            details.append(f"image_source_host={urlparse(self.source_url).netloc or 'unknown'}")
        return details
