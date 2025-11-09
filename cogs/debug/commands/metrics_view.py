from __future__ import annotations

from typing import Optional

from modules.metrics import compute_latency_breakdown

__all__ = ["format_latency_breakdown"]


async def format_latency_breakdown() -> str:
    breakdown = await compute_latency_breakdown()
    overall = breakdown.get("overall") or {}
    video = breakdown.get("video") or {}
    image = breakdown.get("image") or {}

    lines = [
        _format_section("Overall", overall),
        _format_section("Video", video),
        _format_section("Image", image),
    ]

    by_type = breakdown.get("by_type", {})
    extras = [
        _format_section(name.title(), data)
        for name, data in by_type.items()
        if name not in {"video", "image"}
    ]
    if extras:
        lines.append("\nAdditional Media Types:")
        lines.extend(extras)

    return "\n".join(line for line in lines if line)


def _format_section(label: str, payload: dict[str, Optional[float]]) -> str:
    scans = payload.get("scans") or 0
    avg_latency = payload.get("average_latency_ms")
    per_frame = payload.get("average_latency_per_frame_ms")
    frames = payload.get("frames_scanned")

    parts = [f"**{label}**"]
    parts.append(f"- Scans: {scans}")
    if avg_latency is not None:
        parts.append(f"- Avg latency: {avg_latency:.2f} ms")
    else:
        parts.append(f"- Avg latency: n/a")
    if per_frame is not None:
        parts.append(f"- Avg latency/frame: {per_frame:.4f} ms")
    else:
        parts.append(f"- Avg latency/frame: n/a")
    if frames is not None:
        parts.append(f"- Frames scanned: {int(frames)}")
    return "\n".join(parts)
