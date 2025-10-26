from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class MediaWorkItem:
    source: str
    label: str
    url: str
    prefer_video: bool = False
    ext_hint: str | None = None
    tenor: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def cache_key(self) -> str:
        base_key = self.metadata.get("cache_key")
        if base_key:
            return base_key
        return f"url::{self.url}"


class MediaFlagged(Exception):
    __slots__ = ("result",)

    def __init__(self, result: dict[str, Any]):
        super().__init__("media_flagged")
        self.result = result


__all__ = ["MediaWorkItem", "MediaFlagged"]
