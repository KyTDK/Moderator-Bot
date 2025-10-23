from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import discord
from discord.ext import commands

from .config import DEFAULT_PUBLIC_VERIFY_URL


_MISSING = object()


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

    def _format_duration(
        self,
        delta: timedelta | None,
        *,
        guild_id: int | None = None,
    ) -> str:
        duration_texts: dict[str, Any] = self._translate(
            "cogs.captcha.base.duration",
            guild_id=guild_id,
            fallback={
                "few_minutes": "a few minutes",
                "joiner": ", ",
                "seconds": {
                    "one": "{count} second",
                    "other": "{count} seconds",
                },
                "minutes": {
                    "one": "{count} minute",
                    "other": "{count} minutes",
                },
                "hours": {
                    "one": "{count} hour",
                    "other": "{count} hours",
                },
                "days": {
                    "one": "{count} day",
                    "other": "{count} days",
                },
            },
        ) or {}

        if not delta:
            return duration_texts.get("few_minutes", "a few minutes")

        total_seconds = int(delta.total_seconds())
        if total_seconds < 60:
            return self._render_quantity(
                duration_texts.get("seconds", {}),
                total_seconds,
                singular="{count} second",
                plural="{count} seconds",
            )

        minutes, seconds = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        days, hours = divmod(hours, 24)

        parts: list[str] = []
        if days:
            parts.append(
                self._render_quantity(
                    duration_texts.get("days", {}),
                    days,
                    singular="{count} day",
                    plural="{count} days",
                )
            )
        if hours:
            parts.append(
                self._render_quantity(
                    duration_texts.get("hours", {}),
                    hours,
                    singular="{count} hour",
                    plural="{count} hours",
                )
            )
        if minutes:
            parts.append(
                self._render_quantity(
                    duration_texts.get("minutes", {}),
                    minutes,
                    singular="{count} minute",
                    plural="{count} minutes",
                )
            )
        if not parts and seconds:
            parts.append(
                self._render_quantity(
                    duration_texts.get("seconds", {}),
                    seconds,
                    singular="{count} second",
                    plural="{count} seconds",
                )
            )
        joiner = duration_texts.get("joiner", ", ")
        return joiner.join(parts) if parts else duration_texts.get("few_minutes", "a few minutes")

    def _build_description(
        self,
        member: discord.Member,
        grace_text: str | None,
        max_attempts: int | None,
        *,
        location: str | None = None,
    ) -> str:
        guild = member.guild
        guild_id = guild.id if guild else None
        has_grace_period = bool(grace_text and grace_text != "0")
        description_texts: dict[str, Any] = self._translate(
            "cogs.captcha.base.description",
            guild_id=guild_id,
            fallback={
                "greeting": "Hi {member}! To finish joining **{guild}**, ",
                "grace": "please {instruction} within **{grace}**.",
                "no_grace": "please {instruction} when you're ready.",
                "instruction_default": "complete the captcha",
                "attempts": {
                    "one": " You have **{count}** attempt.",
                    "other": " You have **{count}** attempts.",
                },
            },
        ) or {}

        instruction = location or description_texts.get("instruction_default", "complete the captcha")
        greeting = description_texts.get(
            "greeting", "Hi {member}! To finish joining **{guild}**, "
        )
        description = greeting.format(
            member=member.mention,
            guild=guild.name if guild else "the server",
        )

        grace_template = (
            description_texts.get("grace")
            if has_grace_period
            else description_texts.get("no_grace")
        )
        if grace_template:
            description += grace_template.format(
                instruction=instruction,
                grace=grace_text,
            )

        if max_attempts:
            attempts_text = self._render_quantity(
                description_texts.get("attempts", {}),
                max_attempts,
                singular=" You have **{count}** attempt.",
                plural=" You have **{count}** attempts.",
            )
            description += attempts_text
        return description

    def _create_embed(
        self,
        *,
        description: str,
        title: str | None = None,
        footer: str | None | object = _MISSING,
        colour: discord.Colour | None = None,
        guild_id: int | None = None,
    ) -> discord.Embed:
        embed_defaults: dict[str, Any] = self._translate(
            "cogs.captcha.base.embed",
            guild_id=guild_id,
            fallback={
                "title": "Captcha Verification Required",
                "footer": "Powered by Moderator Bot",
            },
        ) or {}

        resolved_title = title or embed_defaults.get("title", "Captcha Verification Required")
        embed = discord.Embed(
            title=resolved_title,
            description=description,
            colour=colour or discord.Color.blurple(),
        )
        if footer is _MISSING:
            footer = embed_defaults.get("footer")
        if footer:
            embed.set_footer(text=footer)
        return embed

    def _build_link_view(
        self,
        url: str,
        *,
        timeout: float | None = None,
        label: str | None = None,
        guild_id: int | None = None,
    ) -> discord.ui.View:
        button_defaults: dict[str, Any] = self._translate(
            "cogs.captcha.base.button",
            guild_id=guild_id,
            fallback={"label": "Verify now"},
        ) or {}
        resolved_label = label or button_defaults.get("label", "Verify now")
        view = discord.ui.View(timeout=timeout)
        view.add_item(
            discord.ui.Button(
                label=resolved_label,
                url=url,
                style=discord.ButtonStyle.link,
            )
        )
        return view

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

    def _render_quantity(
        self,
        forms: dict[str, str],
        count: int,
        *,
        singular: str,
        plural: str,
    ) -> str:
        template = forms.get("one" if count == 1 else "other")
        if not template:
            template = singular if count == 1 else plural
        return template.format(count=count)

    def _translate(
        self,
        key: str,
        *,
        guild_id: int | None = None,
        placeholders: dict[str, Any] | None = None,
        fallback: Any = None,
    ) -> Any:
        bot = getattr(self, "bot", None)
        if bot is not None and hasattr(bot, "translate"):
            return bot.translate(
                key,
                guild_id=guild_id,
                placeholders=placeholders,
                fallback=fallback,
            )
        return fallback
