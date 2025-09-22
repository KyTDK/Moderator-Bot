from __future__ import annotations

import asyncio
import logging
import os
from datetime import timedelta
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import discord
from discord import app_commands, Interaction
from discord.ext import commands
from discord.utils import utcnow

from modules.utils import mysql
from modules.utils.discord_utils import resolve_role_references
from modules.utils.time import parse_duration

from modules.captcha import CaptchaStreamConfig, CaptchaStreamListener
from modules.captcha.client import (
    CaptchaApiClient,
    CaptchaApiError,
    CaptchaNotAvailableError,
    CaptchaGuildConfig,
    CaptchaStartResponse,
)
from modules.captcha.sessions import CaptchaSession, CaptchaSessionStore

_DEFAULT_API_BASE = "https://modbot.neomechanical.com/api/captcha"
_DEFAULT_PUBLIC_VERIFY_URL = "https://modbot.neomechanical.com/captcha"
_logger = logging.getLogger(__name__)

class CaptchaCog(commands.Cog):
    """Captcha verification flow for new guild members."""

    captcha_group = app_commands.Group(
        name="captcha",
        description="Manage captcha verification.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._session_store = CaptchaSessionStore()
        self._api_base = _resolve_api_base()
        self._api_client = CaptchaApiClient(self._api_base, os.getenv("CAPTCHA_API_TOKEN"))
        self._stream_config = CaptchaStreamConfig.from_env()
        self._stream_listener = CaptchaStreamListener(bot, self._stream_config, self._session_store)
        self._public_verify_url = _resolve_public_verify_url()
        self._settings_listener_registered = False
        self._embed_sync_task: asyncio.Task[None] | None = None
        self._config_cache: dict[int, CaptchaGuildConfig] = {}
        self._config_cache_expiry: dict[int, float] = {}
        self._config_cache_ttl = 300.0

        if not self._api_client.is_configured:
            _logger.warning(
                "CAPTCHA_API_TOKEN or API base URL missing; captcha verification will be disabled."
            )

    @captcha_group.command(name="sync", description="Resend the captcha verification embed.")
    async def sync_embed_command(self, interaction: Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            updated = await self._sync_guild_embed(interaction.guild, force=True)
        except Exception:
            _logger.exception(
                "Failed to synchronise captcha embed for guild %s via command", interaction.guild.id
            )
            await interaction.followup.send(
                "An unexpected error occurred while updating the captcha embed.",
                ephemeral=True,
            )
            return

        if updated:
            await interaction.followup.send(
                "Captcha verification embed has been updated.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "Captcha embed delivery is not configured for this server.",
                ephemeral=True,
            )

    async def cog_load(self) -> None:
        started = await self._stream_listener.start()
        if started:
            print(
                "[CAPTCHA] Redis stream listener subscribed to "
                f"{self._stream_config.stream} as "
                f"{self._stream_config.group}/{self._stream_config.consumer_name}"
            )
        else:
            print("[CAPTCHA] Captcha Redis stream listener disabled; callbacks will not be processed.")

        if not self._settings_listener_registered:
            mysql.add_settings_listener(self._handle_setting_update)
            self._settings_listener_registered = True

        if self._embed_sync_task is None:
            self._embed_sync_task = asyncio.create_task(self._initial_sync_embeds())

    async def cog_unload(self) -> None:
        await self._stream_listener.stop()
        await self._api_client.close()
        if self._settings_listener_registered:
            mysql.remove_settings_listener(self._handle_setting_update)
            self._settings_listener_registered = False
        if self._embed_sync_task is not None:
            self._embed_sync_task.cancel()
            try:
                await self._embed_sync_task
            except asyncio.CancelledError:
                pass
            self._embed_sync_task = None

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot or member.guild is None:
            return

        settings = await mysql.get_settings(
            member.guild.id,
            [
                "captcha-verification-enabled",
                "captcha-grace-period",
                "captcha-max-attempts",
                "pre-captcha-roles",
                "captcha-delivery-method",
                "captcha-embed-channel-id",
            ],
        )

        if not settings.get("captcha-verification-enabled"):
            return

        if not self._api_client.is_configured:
            _logger.debug(
                "Captcha API not configured; skipping verification for guild %s", member.guild.id
            )
            return

        # Give pre-captcha roles if configured
        raw_pre_roles = settings.get("pre-captcha-roles") or []
        pre_roles = [
            role
            for role in resolve_role_references(
                member.guild,
                raw_pre_roles,
                allow_names=False,
                logger=_logger,
            )
            if role not in member.roles
        ]
        if pre_roles:
            try:
                await member.add_roles(*pre_roles, reason="Assigning pre-captcha roles")
            except discord.Forbidden:
                _logger.warning(
                    "Missing permissions to assign pre-captcha roles in guild %s",
                    member.guild.id,
                )
            except discord.HTTPException:
                _logger.warning(
                    "Failed to assign pre-captcha roles in guild %s for user %s",
                    member.guild.id,
                    member.id,
                )

        delivery_method = str(settings.get("captcha-delivery-method") or "dm").lower()
        embed_channel_id = self._coerce_positive_int(settings.get("captcha-embed-channel-id"))
        grace_setting = self._coerce_grace_period(settings.get("captcha-grace-period"))
        grace_delta = parse_duration(grace_setting) if grace_setting else None
        if grace_delta is None:
            grace_delta = timedelta(minutes=10)
        grace_display = grace_setting or self._format_duration(grace_delta)
        max_attempts = self._coerce_positive_int(settings.get("captcha-max-attempts"))

        if delivery_method == "embed" and embed_channel_id:
            expires_at = utcnow() + grace_delta
            session = CaptchaSession(
                guild_id=member.guild.id,
                user_id=member.id,
                token=None,
                expires_at=expires_at,
                delivery_method="embed",
            )
            await self._session_store.put(session)

            channel = member.guild.get_channel(embed_channel_id)
            if isinstance(channel, discord.TextChannel):
                await self._sync_guild_embed(member.guild)
                await self._notify_member_embed(
                    member,
                    channel,
                    grace_display,
                    max_attempts,
                )
                return
            else:
                _logger.warning(
                    "Captcha embed channel %s not found in guild %s; falling back to DMs",
                    embed_channel_id,
                    member.guild.id,
                )

        try:
            start_response = await self._api_client.start_session(
                member.guild.id,
                member.id,
            )
        except CaptchaNotAvailableError as exc:
            _logger.info(
                "Captcha not available for guild %s: %s",
                member.guild.id,
                exc,
            )
            return
        except CaptchaApiError as exc:
            _logger.warning(
                "Failed to start captcha session for guild %s user %s: %s",
                member.guild.id,
                member.id,
                exc,
            )
            return
        except Exception:
            _logger.exception(
                "Unexpected error when creating captcha session for guild %s user %s",
                member.guild.id,
                member.id,
            )
            return

        session = CaptchaSession(
            guild_id=start_response.guild_id,
            user_id=start_response.user_id,
            token=start_response.token,
            expires_at=start_response.expires_at,
            state=start_response.state,
            delivery_method="dm",
        )
        await self._session_store.put(session)

        await self._notify_member(
            member,
            start_response,
            grace_period=grace_setting,
            max_attempts=max_attempts,
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if member.guild is None:
            return
        await self._session_store.remove(member.guild.id, member.id)

    async def _notify_member(
        self,
        member: discord.Member,
        response: CaptchaStartResponse,
        *,
        grace_period: str | None,
        max_attempts: int | None,
    ) -> None:
        grace_delta = parse_duration(grace_period) if grace_period else None
        if grace_delta is None:
            grace_delta = timedelta(minutes=10)
        grace_seconds = int(grace_delta.total_seconds()) if grace_delta else 600
        if grace_seconds <= 0:
            grace_seconds = 600

        expires_in: int | None = None
        if response.expires_at:
            remaining = int((response.expires_at - utcnow()).total_seconds())
            expires_in = remaining if remaining > 0 else None

        view_timeout = grace_seconds
        if expires_in is not None:
            view_timeout = max(1, min(grace_seconds, expires_in))

        display_grace = grace_period or self._format_duration(grace_delta)

        # Build an embed
        embed = discord.Embed(
            title="Captcha Verification Required",
            description=self._build_description(member, display_grace, max_attempts),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Powered by Moderator Bot")

        # Build a button view (link style)
        view = discord.ui.View(timeout=float(view_timeout))
        view.add_item(
            discord.ui.Button(
                label="Click here to verify",
                url=response.verification_url,
                style=discord.ButtonStyle.link,
            )
        )

        # Try DM first
        try:
            await member.send(embed=embed, view=view)
            return
        except discord.Forbidden:
            _logger.debug("Could not DM captcha instructions to user %s", member.id)
        except discord.HTTPException:
            _logger.debug("Failed to DM captcha instructions to user %s", member.id)

        # Fallback to a guild channel
        channel = self._find_fallback_channel(member.guild)
        if channel is None:
            return

        try:
            await channel.send(
                content=member.mention,
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except discord.HTTPException:
            _logger.debug(
                "Failed to post captcha instructions in guild %s for user %s",
                member.guild.id,
                member.id,
            )

    def _find_fallback_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        me = guild.me
        if me is None and self.bot.user is not None:
            me = guild.get_member(self.bot.user.id)

        if me is None:
            return None

        if guild.system_channel and guild.system_channel.permissions_for(me).send_messages:
            return guild.system_channel

        for channel in guild.text_channels:
            if channel.permissions_for(me).send_messages:
                return channel

        return None

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
        grace_text: str,
        max_attempts: int | None,
    ) -> str:
        description = (
            f"Hi {member.mention}! To finish joining **{member.guild.name}**, "
            f"please complete the captcha within **{grace_text}**."
        )
        if max_attempts:
            attempt_label = "attempt" if max_attempts == 1 else "attempts"
            description += f" You have **{max_attempts}** {attempt_label}."
        return description

    def _build_public_verification_url(self, guild_id: int) -> str:
        base = self._public_verify_url or _DEFAULT_PUBLIC_VERIFY_URL
        parts = urlsplit(base)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["guildId"] = str(guild_id)
        new_query = urlencode(query)
        rebuilt = parts._replace(query=new_query)
        return urlunsplit(rebuilt)

    async def _initial_sync_embeds(self) -> None:
        await self.bot.wait_until_ready()
        for guild in list(self.bot.guilds):
            try:
                await self._sync_guild_embed(guild)
            except Exception:  # pragma: no cover - defensive logging
                _logger.exception(
                    "Failed to synchronise captcha embed for guild %s", guild.id
                )

    async def _handle_setting_update(self, guild_id: int, key: str, value: object) -> None:
        if key not in {
            "captcha-delivery-method",
            "captcha-embed-channel-id",
            "captcha-verification-enabled",
        }:
            return

        try:
            resolved_id = int(guild_id)
        except (TypeError, ValueError):
            return

        guild = self.bot.get_guild(resolved_id)
        if guild is None:
            return

        try:
            await self._sync_guild_embed(guild, force=True)
        except Exception:  # pragma: no cover - defensive logging
            _logger.exception("Failed to update captcha embed for guild %s", resolved_id)

    async def _sync_guild_embed(
        self,
        guild: discord.Guild,
        *,
        force: bool = False,
    ) -> bool:
        settings = await mysql.get_settings(
            guild.id,
            [
                "captcha-verification-enabled",
                "captcha-delivery-method",
                "captcha-embed-channel-id",
            ],
        )

        enabled = bool(settings.get("captcha-verification-enabled"))
        delivery_method = str(settings.get("captcha-delivery-method") or "dm").lower()
        channel_id = self._coerce_positive_int(settings.get("captcha-embed-channel-id"))

        if not enabled or delivery_method != "embed" or not channel_id:
            await self._remove_embed_message(guild)
            return False

        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            await self._remove_embed_message(guild)
            return False

        config = await self._fetch_guild_config(guild.id)
        provider_label = None
        requires_login = True
        if config:
            provider_label = config.provider_label or config.provider
            requires_login = config.delivery.requires_login

        return await self._ensure_embed_message(
            guild,
            channel,
            provider_label,
            requires_login,
            force=force,
        )

    async def _ensure_embed_message(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        provider_label: str | None,
        requires_login: bool,
        *,
        force: bool = False,
    ) -> bool:
        record = await mysql.get_captcha_embed_record(guild.id)
        embed, view = self._build_verification_embed(
            guild,
            provider_label,
            requires_login,
        )

        if record and record.channel_id == channel.id and not force:
            try:
                message = await channel.fetch_message(record.message_id)
            except (discord.NotFound, discord.HTTPException, discord.Forbidden):
                message = None
            if message is not None:
                try:
                    await message.edit(embed=embed, view=view)
                except discord.HTTPException:
                    _logger.warning(
                        "Failed to update captcha embed message for guild %s in channel %s",
                        guild.id,
                        channel.id,
                    )
                    return False
                await mysql.upsert_captcha_embed_record(guild.id, channel.id, message.id)
                return True

        if record:
            await self._remove_embed_message(guild)

        try:
            message = await channel.send(
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            _logger.warning(
                "Missing permissions to send captcha embed in guild %s channel %s",
                guild.id,
                channel.id,
            )
            return False
        except discord.HTTPException:
            _logger.warning(
                "Failed to send captcha embed in guild %s channel %s",
                guild.id,
                channel.id,
            )
            return False

        await mysql.upsert_captcha_embed_record(guild.id, channel.id, message.id)
        return True

    async def _remove_embed_message(self, guild: discord.Guild) -> None:
        record = await mysql.get_captcha_embed_record(guild.id)
        if not record:
            return

        channel = guild.get_channel(record.channel_id)
        if isinstance(channel, discord.TextChannel):
            try:
                message = await channel.fetch_message(record.message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                message = None
            if message is not None:
                try:
                    await message.delete()
                except discord.HTTPException:
                    _logger.debug(
                        "Failed to delete existing captcha embed message in guild %s",
                        guild.id,
                    )

        await mysql.delete_captcha_embed_record(guild.id)

    def _build_verification_embed(
        self,
        guild: discord.Guild,
        provider_label: str | None,
        requires_login: bool,
    ) -> tuple[discord.Embed, discord.ui.View]:
        description = (
            "Click the button below to verify yourself. Once you pass the captcha, "
            "you will gain access to the rest of the server."
        )
        if requires_login:
            description += " You may be asked to log in to the Moderator Bot dashboard first."

        embed = discord.Embed(
            title="Complete Captcha Verification",
            description=description,
            colour=discord.Colour.blurple(),
        )
        if provider_label:
            embed.add_field(name="Provider", value=provider_label, inline=True)
        embed.set_footer(text=f"Guild ID: {guild.id} • Powered by Moderator Bot")

        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                label="Verify now",
                url=self._build_public_verification_url(guild.id),
                style=discord.ButtonStyle.link,
            )
        )
        return embed, view

    async def _fetch_guild_config(self, guild_id: int) -> CaptchaGuildConfig | None:
        if not self._api_client.is_configured:
            return None

        loop = asyncio.get_running_loop()
        now = loop.time()
        expires_at = self._config_cache_expiry.get(guild_id)
        if expires_at and expires_at > now:
            return self._config_cache.get(guild_id)

        try:
            config = await self._api_client.fetch_guild_config(guild_id)
        except (CaptchaApiError, CaptchaNotAvailableError) as exc:
            _logger.debug(
                "Failed to fetch captcha configuration for guild %s: %s",
                guild_id,
                exc,
            )
            self._config_cache.pop(guild_id, None)
            self._config_cache_expiry.pop(guild_id, None)
            return None

        self._config_cache[guild_id] = config
        self._config_cache_expiry[guild_id] = now + self._config_cache_ttl
        return config

    async def _notify_member_embed(
        self,
        member: discord.Member,
        channel: discord.TextChannel,
        grace_text: str,
        max_attempts: int | None,
    ) -> None:
        url = self._build_public_verification_url(member.guild.id)
        description = (
            f"Hi {member.mention}! To finish joining **{member.guild.name}**, please visit "
            f"{channel.mention} and complete the captcha within **{grace_text}**."
        )
        if max_attempts:
            attempt_label = "attempt" if max_attempts == 1 else "attempts"
            description += f" You have **{max_attempts}** {attempt_label}."

        embed = discord.Embed(
            title="Captcha Verification Required",
            description=description,
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Need help?",
            value="Click the button below to open the verification page.",
            inline=False,
        )
        embed.set_footer(text="Powered by Moderator Bot")

        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                label="Open verification page",
                url=url,
                style=discord.ButtonStyle.link,
            )
        )

        try:
            await member.send(embed=embed, view=view)
        except (discord.Forbidden, discord.HTTPException):
            _logger.debug("Failed to DM captcha embed instructions to user %s", member.id)

def _resolve_api_base() -> str:
    raw = os.getenv("CAPTCHA_PUBLIC_VERIFY_URL")
    if not raw:
        return _DEFAULT_API_BASE

    base = raw.strip()
    if not base:
        return _DEFAULT_API_BASE

    parts = urlsplit(base)
    path = parts.path

    if path.endswith("/start"):
        path = path[: -len("/start")]

    path = path.rstrip("/")

    if path.endswith("/accelerated/captcha"):
        path = f"{path[: -len('/accelerated/captcha')]}/api/captcha"
    elif path.endswith("/captcha"):
        path = f"{path[: -len('/captcha')]}/api/captcha"
    elif not path.endswith("/api/captcha"):
        if path:
            path = f"{path}/api/captcha"
        else:
            path = "/api/captcha"

    rebuilt = parts._replace(path=path, query="", fragment="")
    return urlunsplit(rebuilt).rstrip("/") or _DEFAULT_API_BASE


def _resolve_public_verify_url() -> str:
    raw = os.getenv("CAPTCHA_PUBLIC_VERIFY_URL")
    if not raw:
        return _DEFAULT_PUBLIC_VERIFY_URL

    cleaned = raw.strip()
    return cleaned or _DEFAULT_PUBLIC_VERIFY_URL

async def setup(bot: commands.Bot) -> None:
    cog = CaptchaCog(bot)
    await bot.add_cog(cog)
