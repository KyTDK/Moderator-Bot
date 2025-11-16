from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional

__all__ = [
    "FeatureStatus",
    "FeatureState",
    "HealthSnapshot",
    "report_feature",
    "get_health_snapshot",
    "render_health_lines",
]


class FeatureStatus(str, Enum):
    """Severity-aware status used to describe feature health."""

    OK = "ok"
    DEGRADED = "degraded"
    DISABLED = "disabled"
    UNAVAILABLE = "unavailable"


_STATUS_RANK = {
    FeatureStatus.OK: 0,
    FeatureStatus.DEGRADED: 1,
    FeatureStatus.DISABLED: 2,
    FeatureStatus.UNAVAILABLE: 3,
}

_STATUS_MARKER = {
    FeatureStatus.OK: "[OK]",
    FeatureStatus.DEGRADED: "[WARN]",
    FeatureStatus.DISABLED: "[DISABLED]",
    FeatureStatus.UNAVAILABLE: "[ERROR]",
}

_STATUS_DISPLAY = {
    FeatureStatus.OK: "OK",
    FeatureStatus.DEGRADED: "Degraded",
    FeatureStatus.DISABLED: "Disabled",
    FeatureStatus.UNAVAILABLE: "Unavailable",
}

_STATUS_ORDER = (
    FeatureStatus.UNAVAILABLE,
    FeatureStatus.DISABLED,
    FeatureStatus.DEGRADED,
    FeatureStatus.OK,
)


@dataclass(slots=True)
class FeatureState:
    """Represents the runtime health of a feature or integration."""

    key: str
    label: str
    category: str
    status: FeatureStatus
    detail: Optional[str] = None
    remedy: Optional[str] = None
    using_fallback: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)

    def copy(self) -> "FeatureState":
        return FeatureState(
            key=self.key,
            label=self.label,
            category=self.category,
            status=self.status,
            detail=self.detail,
            remedy=self.remedy,
            using_fallback=self.using_fallback,
            metadata=dict(self.metadata),
            updated_at=self.updated_at,
        )


@dataclass(slots=True)
class HealthSnapshot:
    """Immutable view of the registry."""

    features: List[FeatureState]
    counts: Dict[FeatureStatus, int]
    generated_at: float

    def overall_status(self) -> FeatureStatus:
        if not self.features:
            return FeatureStatus.OK
        worst = max(self.features, key=lambda item: _STATUS_RANK[item.status])
        return worst.status

    def fallback_features(self) -> List[FeatureState]:
        return [feature for feature in self.features if feature.using_fallback]


class _HealthRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._features: Dict[str, FeatureState] = {}

    def update_feature(
        self,
        key: str,
        *,
        label: Optional[str] = None,
        status: FeatureStatus,
        category: str = "general",
        detail: Optional[str] = None,
        remedy: Optional[str] = None,
        using_fallback: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> FeatureState:
        metadata = metadata or {}
        with self._lock:
            state = self._features.get(key)
            if state is None:
                state = FeatureState(
                    key=key,
                    label=label or key,
                    category=category,
                    status=status,
                )
                self._features[key] = state

            state.label = label or state.label or key
            state.category = category
            state.status = status
            state.detail = detail
            state.remedy = remedy
            state.using_fallback = using_fallback
            state.metadata = metadata
            state.updated_at = time.time()
            return state

    def snapshot(self) -> HealthSnapshot:
        with self._lock:
            features = [item.copy() for item in self._features.values()]

        ordered = sorted(
            features,
            key=lambda feature: (-_STATUS_RANK[feature.status], feature.label.lower()),
        )
        counts: Dict[FeatureStatus, int] = {status: 0 for status in FeatureStatus}
        for feature in ordered:
            counts[feature.status] = counts.get(feature.status, 0) + 1
        return HealthSnapshot(features=ordered, counts=counts, generated_at=time.time())

    def bulk_update(self, entries: Iterable[FeatureState]) -> None:
        with self._lock:
            for state in entries:
                self._features[state.key] = state.copy()


_REGISTRY = _HealthRegistry()


def report_feature(
    key: str,
    *,
    label: Optional[str] = None,
    status: FeatureStatus,
    category: str = "general",
    detail: Optional[str] = None,
    remedy: Optional[str] = None,
    using_fallback: bool = False,
    metadata: Optional[Dict[str, Any]] = None,
) -> FeatureState:
    """Update the recorded health for `key`."""

    return _REGISTRY.update_feature(
        key,
        label=label,
        status=status,
        category=category,
        detail=detail,
        remedy=remedy,
        using_fallback=using_fallback,
        metadata=metadata,
    )


def get_health_snapshot() -> HealthSnapshot:
    """Return a point-in-time copy of the registry."""

    return _REGISTRY.snapshot()


def format_status_counts(
    snapshot: HealthSnapshot,
    *,
    include_ok: bool = False,
) -> str:
    """Return a comma-separated list of counts per status."""

    parts: List[str] = []
    for status in _STATUS_ORDER:
        if not include_ok and status is FeatureStatus.OK:
            continue
        count = snapshot.counts.get(status, 0)
        if count:
            parts.append(f"{_STATUS_DISPLAY[status]}: {count}")
    return ", ".join(parts)


def render_health_lines(
    snapshot: HealthSnapshot,
    *,
    per_status_limit: int = 4,
    include_ok: bool = False,
) -> List[str]:
    """
    Convert a snapshot into structured summary lines grouped by status.

    Only degraded/disabled/unavailable features are shown by default.
    """

    lines: List[str] = []
    for status in _STATUS_ORDER:
        if not include_ok and status is FeatureStatus.OK:
            continue
        matching = [
            feature for feature in snapshot.features if feature.status is status
        ]
        if not matching:
            continue
        header = f"{_STATUS_DISPLAY[status]} ({len(matching)})"
        lines.append(header)
        for feature in matching[:per_status_limit]:
            fallback_note = " [fallback]" if feature.using_fallback else ""
            detail = f" — {feature.detail}" if feature.detail else ""
            lines.append(f"  - {feature.label}{fallback_note}{detail}")
        remaining = len(matching) - per_status_limit
        if remaining > 0:
            lines.append(f"  - ...and {remaining} more.")
        lines.append("")

    if not lines:
        return ["OK (0)", "  - All monitored subsystems report OK."]

    if lines[-1] == "":
        lines.pop()
    return lines
