from __future__ import annotations

from typing import Iterable

import discord

from .media_rates import MediaProcessingRate, MediaRateCalculator
from .queue_snapshot import QueueSnapshot


def _format_processing_rates_field(
    *,
    rates: Iterable[MediaProcessingRate],
    calculator: MediaRateCalculator,
) -> tuple[str, str]:
    label = f"Processing rates (last {calculator.window_minutes:.1f}m)"
    value = MediaRateCalculator.format_rates_for_embed(rates, calculator.window_minutes)
    return label, value


def _add_queue_fields(
    embed: discord.Embed,
    *,
    free: QueueSnapshot,
    accel: QueueSnapshot,
) -> None:
    embed.add_field(name="Free queue", value=free.format_lines(), inline=False)
    embed.add_field(name="Accelerated queue", value=accel.format_lines(), inline=False)


def build_backlog_embed(
    *,
    free: QueueSnapshot,
    accel: QueueSnapshot,
    dropped_delta: int,
    rates: Iterable[MediaProcessingRate],
    calculator: MediaRateCalculator,
) -> discord.Embed:
    ratio = free.backlog_ratio
    description = (
        f"Free backlog {free.backlog} (~{ratio:.2f}x high watermark)"
        if free.backlog_high
        else f"Free backlog {free.backlog}"
    )

    embed = discord.Embed(
        title="Free queue backlog warning",
        description=description,
        color=discord.Color.orange(),
    )
    _add_queue_fields(embed, free=free, accel=accel)
    embed.add_field(
        name="Current tuning snapshot",
        value="\n".join(
            [
                (
                    "FREE workers: "
                    f"base {free.baseline_workers} / current {free.max_workers} / "
                    f"burst {free.autoscale_max}"
                ),
                (
                    "ACCEL workers: "
                    f"base {accel.baseline_workers} / current {accel.max_workers} / "
                    f"burst {accel.autoscale_max}"
                ),
                (
                    "Watermarks: "
                    f"high={free.backlog_high} / {accel.backlog_high}, "
                    f"low={free.backlog_low} / {accel.backlog_low}"
                ),
            ]
        ),
        inline=False,
    )
    embed.add_field(
        name="Longest free task breakdown",
        value=free.format_longest_runtime_detail(),
        inline=False,
    )
    embed.add_field(
        name="Latest free task snapshot",
        value=free.format_last_runtime_detail(),
        inline=False,
    )
    embed.set_footer(text=f"Dropped tasks since last report: {dropped_delta}")

    label, value = _format_processing_rates_field(rates=rates, calculator=calculator)
    embed.add_field(name=label, value=value, inline=False)

    return embed


def build_backlog_cleared_embed(
    *,
    free: QueueSnapshot,
    accel: QueueSnapshot,
    rates: Iterable[MediaProcessingRate],
    calculator: MediaRateCalculator,
) -> discord.Embed:
    description = (
        "Free queue backlog has cleared."
        if free.backlog <= 0
        else f"Free backlog reduced to {free.backlog}."
    )
    embed = discord.Embed(
        title="Free queue backlog recovered",
        description=description,
        color=discord.Color.green(),
    )
    _add_queue_fields(embed, free=free, accel=accel)

    label, value = _format_processing_rates_field(rates=rates, calculator=calculator)
    embed.add_field(name=label, value=value, inline=False)

    return embed


__all__ = [
    "build_backlog_cleared_embed",
    "build_backlog_embed",
]
