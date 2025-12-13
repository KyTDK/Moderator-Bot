from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Optional

from modules.utils.time import parse_duration


@dataclass(slots=True)
class VoiceSettings:
    enabled: bool
    channel_ids: list[int]
    category_ids: list[int]
    saver_mode: bool
    listen_delta: timedelta
    idle_delta: timedelta
    high_accuracy: bool
    high_quality_transcription: bool
    rules: str
    action_setting: list[str]
    aimod_debug: bool
    log_channel: Optional[int]
    transcript_channel_id: Optional[int]
    transcript_only: bool
    join_announcement: bool

    @classmethod
    def from_raw(cls, settings: dict[str, Any]) -> "VoiceSettings":
        enabled = bool(settings.get("vcmod-enabled"))
        saver_mode = bool(settings.get("vcmod-saver-mode"))
        high_accuracy = bool(settings.get("vcmod-high-accuracy"))
        high_quality = bool(settings.get("vcmod-high-quality-transcription"))
        transcript_only = bool(settings.get("vcmod-transcript-only"))
        join_announcement = bool(settings.get("vcmod-join-announcement"))

        listen_delta = parse_duration(settings.get("vcmod-listen-duration") or "2m") or timedelta(minutes=2)
        idle_delta = parse_duration(settings.get("vcmod-idle-duration") or "30s") or timedelta(seconds=30)

        channels_raw = settings.get("vcmod-channels") or []
        channel_ids: list[int] = []
        for entry in channels_raw:
            try:
                cid = int(getattr(entry, "id", entry))
            except Exception:
                continue
            channel_ids.append(cid)

        category_ids: list[int] = []
        categories_raw = settings.get("vcmod-categories") or []

        for entry in categories_raw:
            try:
                cid = int(getattr(entry, "id", entry))
            except Exception:
                continue
            category_ids.append(cid)

        rules = settings.get("vcmod-rules") or ""
        action_setting = list(settings.get("vcmod-detection-action") or ["auto"])
        aimod_debug = bool(settings.get("aimod-debug"))
        log_channel = settings.get("aimod-channel") or settings.get("monitor-channel")
        transcript_channel_id = settings.get("vcmod-transcript-channel")

        return cls(
            enabled=enabled,
            channel_ids=channel_ids,
            category_ids=category_ids,
            saver_mode=saver_mode,
            listen_delta=listen_delta,
            idle_delta=idle_delta,
            high_accuracy=high_accuracy,
            high_quality_transcription=high_quality,
            rules=rules,
            action_setting=action_setting,
            aimod_debug=aimod_debug,
            log_channel=log_channel,
            transcript_channel_id=transcript_channel_id,
            transcript_only=transcript_only,
            join_announcement=join_announcement,
        )
