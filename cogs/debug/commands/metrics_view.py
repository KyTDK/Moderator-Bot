from __future__ import annotations

from typing import Optional

from modules.metrics import compute_latency_breakdown

__all__ = ["format_latency_breakdown"]


async def format_latency_breakdown() -> str:
    breakdown = await compute_latency_breakdown()
    overall = breakdown.get("overall") or {}
    video = breakdown.get("video") or {}
    image = breakdown.get("image") or {}

    sections = [
        _format_section("Overall", overall),
        _format_section("Video", video),
        _format_section("Image", image),
    ]

    by_type = breakdown.get("by_type", {})
    extras = [
        (name, data)
        for name, data in by_type.items()
        if name not in {"video", "image"}
    ]
    extras.sort(key=lambda item: item[1].get("scans") or 0, reverse=True)
    extras = extras[:5]

    if extras:
        sections.append("__Additional Media Types__")
        sections.extend(
            _format_section(name.title(), data)
            for name, data in extras
        )

    return "\n".join(section for section in sections if section)


def _format_section(label: str, payload: dict[str, Optional[float]]) -> str:
    scans = payload.get("scans") or 0
    avg_latency = payload.get("average_latency_ms")
    per_frame = payload.get("average_latency_per_frame_ms")
    frames = payload.get("frames_scanned")

    parts = [f"**{label}**"]
    parts.append(f"- Scans: {scans:,}")
    if avg_latency is not None:
        parts.append(f"- Avg latency: {avg_latency:.2f} ms")
    else:
        parts.append(f"- Avg latency: n/a")
    if per_frame is not None:
        parts.append(f"- Avg latency/frame: {per_frame:.4f} ms")
    else:
        parts.append(f"- Avg latency/frame: n/a")
    if frames is not None:
        parts.append(f"- Frames scanned: {int(frames):,}")
    return "\n".join(parts)
