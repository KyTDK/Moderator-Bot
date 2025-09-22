from __future__ import annotations

from datetime import timedelta
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import discord
from discord.ext import commands

from .config import DEFAULT_PUBLIC_VERIFY_URL


class CaptchaBaseMixin:
    """Shared helpers for captcha functionality."""

    _public_verify_url: str
    bot: commands.Bot  # type: ignore[assignment]

    @staticmethod
    def _coerce_positive_int(value: object) -> int | None:
        try:
            number = int(str(value))
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    @staticmethod
    def _coerce_grace_period(value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _format_duration(delta: timedelta | None) -> str:
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

    def _build_description(
        self,
        member: discord.Member,
        grace_text: str | None,
        max_attempts: int | None,
    ) -> str:
        has_grace_period = bool(grace_text and grace_text != "0")
        description = (
            f"Hi {member.mention}! To finish joining **{member.guild.name}**, "
            + (
                f"please complete the captcha within **{grace_text}**."
                if has_grace_period
                else "please complete the captcha when you're ready."
            )
        )
        if max_attempts:
            attempt_label = "attempt" if max_attempts == 1 else "attempts"
            description += f" You have **{max_attempts}** {attempt_label}."
        return description

    def _build_public_verification_url(self, guild_id: int) -> str:
        base = self._public_verify_url or DEFAULT_PUBLIC_VERIFY_URL
        parts = urlsplit(base)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["guildId"] = str(guild_id)
        new_query = urlencode(query)
        rebuilt = parts._replace(query=new_query)
        return urlunsplit(rebuilt)

    def _find_fallback_channel(
        self, guild: discord.Guild
    ) -> Optional[discord.TextChannel]:
        me = guild.me
        if me is None and getattr(self, "bot", None) is not None:
            me = guild.get_member(self.bot.user.id) if self.bot.user else None

        if me is None:
            return None

        if guild.system_channel and guild.system_channel.permissions_for(me).send_messages:
            return guild.system_channel

        for channel in guild.text_channels:
            if channel.permissions_for(me).send_messages:
                return channel

        return None
