from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .latency import ModeratorLatencyTracker
from .moderation_logging import report_remote_payload_failure, truncate_text
from .moderation_state import ImageModerationState

__all__ = ["RemoteFallbackContext"]


def _using_remote_inputs(
    has_image_input: bool,
    image_state: ImageModerationState | None,
) -> bool:
    return (
        has_image_input
        and isinstance(image_state, ImageModerationState)
        and bool(image_state.use_remote)
        and bool(image_state.source_url)
    )


@dataclass(slots=True)
class RemoteFallbackContext:
    scanner: Any
    has_image_input: bool
    image_state: ImageModerationState | None
    latency_tracker: ModeratorLatencyTracker
    payload_metadata: dict[str, Any] | None
    metadata_dict: dict[str, Any] | None
    max_attempts: int

    def using_remote_inputs(self) -> bool:
        return _using_remote_inputs(self.has_image_input, self.image_state)

    def record_fallback_context(self, label: str, message: str | None = None) -> None:
        if not isinstance(self.payload_metadata, dict):
            return
        text = (message or "").strip() if isinstance(message, str) else ""
        if text:
            text = truncate_text(text, 512)
        entry = f"{label}: {text}" if text else label
        contexts = self.payload_metadata.setdefault("fallback_contexts", [])
        if entry not in contexts:
            contexts.append(entry)

    async def handle_remote_inline_fallback(
        self,
        *,
        label: str,
        error_message: str,
        attempt_number: int,
        context_summary: Optional[str] = None,
        failure_reason: Optional[str] = None,
    ) -> bool:
        if not self.using_remote_inputs():
            return False

        cleaned_error = error_message.strip() if isinstance(error_message, str) else str(error_message)
        if failure_reason:
            self.latency_tracker.record_failure(failure_reason)
        self.latency_tracker.set_payload_detail("remote_inline_retry", True)
        self.latency_tracker.ensure_payload_detail("remote_failure_label", label)

        if isinstance(self.payload_metadata, dict):
            self.payload_metadata["remote_inline_retry"] = True
            failure_labels = self.payload_metadata.setdefault("remote_failure_labels", [])
            if label not in failure_labels:
                failure_labels.append(label)

        if isinstance(self.image_state, ImageModerationState):
            self.image_state.mark_fallback(label)
            self.image_state.force_inline()

        self.record_fallback_context(label, cleaned_error)
        latency_snapshot = self.latency_tracker.snapshot()
        if isinstance(self.metadata_dict, dict):
            self.metadata_dict["moderation_tracker"] = latency_snapshot

        await report_remote_payload_failure(
            self.scanner,
            attempt_number=attempt_number,
            max_attempts=self.max_attempts,
            error_message=cleaned_error,
            image_state=self.image_state if isinstance(self.image_state, ImageModerationState) else None,
            payload_metadata=self.metadata_dict,
            latency_snapshot=latency_snapshot,
            context_summary=context_summary,
        )
        return True
