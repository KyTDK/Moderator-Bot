from __future__ import annotations

import time
from collections import Counter
from typing import Any


class ModeratorLatencyTracker:
    """Collect and expose detailed latency metrics for moderator API calls."""

    _BREAKDOWN_LABELS: dict[str, tuple[str, str]] = {
        "payload_prepare_ms": ("moderation_payload", "Moderator Payload Prep"),
        "key_acquire_ms": ("moderation_key_acquire", "API Client Acquire"),
        "key_wait_ms": ("moderation_key_wait", "API Key Wait"),
        "resource_latency_ms": ("moderation_resource", "Moderator Client Resolve"),
        "api_call_ms": ("moderation_request", "Moderator API Request"),
        "response_parse_ms": ("moderation_response_parse", "Moderator Response Parse"),
        "vector_add_ms": ("moderation_vector", "Vector Maintenance"),
    }

    def __init__(self) -> None:
        self.timings: dict[str, float] = {
            key: 0.0 for key in self._BREAKDOWN_LABELS.keys()
        }
        self._metrics: dict[str, Any] = {
            "attempts": 0,
            "no_key_waits": 0,
            "failures": Counter(),
        }
        self._successful_attempt: bool = False
        self._payload_details: dict[str, Any] = {}

    def start(self, key: str) -> float:
        return time.perf_counter()

    def stop(self, key: str, started: float | None) -> None:
        if started is None:
            return
        self.timings[key] = self.timings.get(key, 0.0) + (
            time.perf_counter() - started
        ) * 1000

    def record_attempt(self) -> None:
        self._metrics["attempts"] += 1

    def record_success(self) -> None:
        self._successful_attempt = True

    def record_no_key_wait(self) -> None:
        self._metrics["no_key_waits"] += 1

    def record_failure(self, reason: str) -> None:
        self._metrics["failures"][reason] += 1

    def merge_payload_details(self, details: dict[str, Any] | None) -> None:
        if not details:
            return
        for key, value in details.items():
            if value is None:
                continue
            self._payload_details[key] = value

    def ensure_payload_detail(self, key: str, value: Any) -> None:
        if value is None or key in self._payload_details:
            return
        self._payload_details[key] = value

    def set_payload_detail(self, key: str, value: Any) -> None:
        if value is None:
            return
        self._payload_details[key] = value

    def finalize(self, payload: dict[str, Any]) -> dict[str, Any]:
        breakdown: dict[str, dict[str, Any]] = {}
        for raw_key, (output_key, label) in self._BREAKDOWN_LABELS.items():
            duration = self.timings.get(raw_key, 0.0)
            if duration > 0:
                breakdown[output_key] = {
                    "duration_ms": duration,
                    "label": label,
                }

        if breakdown:
            pipeline_metrics = payload.setdefault("pipeline_metrics", {})
            moderator_breakdown = pipeline_metrics.setdefault(
                "moderator_breakdown_ms", {}
            )
            moderator_breakdown.update(breakdown)

        metadata: dict[str, Any] = {}
        if self._metrics["attempts"]:
            metadata["attempts"] = self._metrics["attempts"]
        if self._metrics["no_key_waits"]:
            metadata["no_key_waits"] = self._metrics["no_key_waits"]
        if self._metrics["failures"]:
            metadata["failures"] = dict(self._metrics["failures"])
        metadata["had_successful_attempt"] = self._successful_attempt
        if self._payload_details:
            metadata["payload_info"] = self._payload_details

        if metadata:
            pipeline_metrics = payload.setdefault("pipeline_metrics", {})
            moderator_metadata = pipeline_metrics.setdefault(
                "moderator_metadata", {}
            )
            moderator_metadata.update(metadata)

        return payload
