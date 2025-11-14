from __future__ import annotations

from typing import Iterable

import discord

from .media_rates import MediaProcessingRate, MediaRateCalculator
from .queue_snapshot import QueueSnapshot


def _format_rates_field(
    *,
    rates: Iterable[MediaProcessingRate],
    calculator: MediaRateCalculator,
) -> tuple[str, str]:
    label = f"Processing rates (last {calculator.window_minutes:.1f}m)"
    value = MediaRateCalculator.format_rates_for_embed(rates, calculator.window_minutes)
    return label, value


def build_performance_alert_embed(
    *,
    free: QueueSnapshot,
    accel: QueueSnapshot,
    comparison,
    rates: Iterable[MediaProcessingRate],
    calculator: MediaRateCalculator,
) -> discord.Embed:
    description_lines = [
        "Accelerated queue throughput is approaching the core path.",
        f"Runtime ratio (accelerated/free): {comparison.runtime_ratio:.2f}",
    ]
    if comparison.wait_ratio > 0:
        description_lines.append(f"Wait ratio (accelerated/free): {comparison.wait_ratio:.2f}")

    embed = discord.Embed(
        title="Accelerated queue slowdown detected",
        description="\n".join(description_lines),
        color=discord.Color.red(),
    )
    embed.add_field(
        name="Runtime comparison",
        value="\n".join(
            [
                f"Accelerated avg: {comparison.accel_runtime:.2f}s",
                f"Free avg: {comparison.free_runtime:.2f}s",
                f"Delta: {comparison.accel_runtime - comparison.free_runtime:+.2f}s",
            ]
        ),
        inline=False,
    )
    embed.add_field(
        name="Wait time comparison",
        value="\n".join(
            [
                f"Accelerated avg wait: {comparison.accel_wait:.2f}s",
                f"Free avg wait: {comparison.free_wait:.2f}s",
                f"Ratio: {comparison.wait_ratio:.2f}",
            ]
        ),
        inline=False,
    )
    embed.add_field(
        name="Trigger reasons",
        value="\n".join(comparison.reasons),
        inline=False,
    )
    embed.add_field(name="Free queue snapshot", value=free.format_lines(), inline=False)
    embed.add_field(name="Accelerated queue snapshot", value=accel.format_lines(), inline=False)
    embed.add_field(
        name="Latest accelerated task",
        value=accel.format_last_runtime_detail(),
        inline=False,
    )
    embed.add_field(
        name="Longest accelerated task",
        value=accel.format_longest_runtime_detail(),
        inline=False,
    )
    embed.add_field(
        name="Latest free task",
        value=free.format_last_runtime_detail(),
        inline=False,
    )

    label, value = _format_rates_field(rates=rates, calculator=calculator)
    embed.add_field(name=label, value=value, inline=False)

    return embed


__all__ = ["build_performance_alert_embed"]
