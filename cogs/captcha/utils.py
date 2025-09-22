from __future__ import annotations

from datetime import timedelta

import discord

def coerce_positive_int(value: object) -> int | None:
    """Convert arbitrary input to a positive integer if possible."""

    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None

def coerce_grace_period(value: object) -> str | None:
    """Normalise grace period configuration values."""

    if value is None:
        return None
    text = str(value).strip()
    return text or None

def format_duration(delta: timedelta | None) -> str:
    """Human friendly duration rendering used in user facing messages."""

    if not delta:
        return "a few minutes"

    total_seconds = int(delta.total_seconds())
    if total_seconds < 60:
        return f"{total_seconds} second{'s' if total_seconds != 1 else ''}"

    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)

    parts: list[str] = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if not parts and seconds:
        parts.append(f"{seconds} second{'s' if seconds != 1 else ''}")
    return ", ".join(parts)

def build_dm_description(
    member: discord.Member,
    grace_text: str,
    max_attempts: int | None,
) -> str:
    """Generate the standard DM instructions for a captcha challenge."""

    description = (
        f"Hi {member.mention}! To finish joining **{member.guild.name}**, "
        f"please complete the captcha within **{grace_text}**."
    )
    if max_attempts:
        attempt_label = "attempt" if max_attempts == 1 else "attempts"
        description += f" You have **{max_attempts}** {attempt_label}."
    return description

def build_embed_delivery_description(
    member: discord.Member,
    channel: discord.TextChannel,
    grace_text: str,
    max_attempts: int | None,
) -> str:
    """Generate instructions sent to users when using the embed flow."""

    description = (
        f"Hi {member.mention}! To finish joining **{member.guild.name}**, please "
        f"visit {channel.mention} and complete the captcha within **{grace_text}**."
    )
    if max_attempts:
        attempt_label = "attempt" if max_attempts == 1 else "attempts"
        description += f" You have **{max_attempts}** {attempt_label}."
    return description