from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

import discord
from discord import Interaction, app_commands
from discord.ext import commands
from discord.utils import utcnow

from modules.captcha import (
    CaptchaCallbackPayload,
    CaptchaCallbackProcessor,
    CaptchaProcessingError,
    CaptchaStreamConfig,
    CaptchaStreamListener,
)
from modules.captcha.client import CaptchaApiClient, CaptchaGuildConfig
from modules.captcha.sessions import CaptchaSessionStore
from modules.i18n.strings import locale_namespace
from modules.utils import mysql
from modules.utils.discord_utils import resolve_role_references
from modules.utils.time import parse_duration
from modules.verification.actions import (
    apply_role_actions,
    parse_role_actions,
)

from .config import resolve_api_base, resolve_public_verify_url
from .delivery import CaptchaDeliveryMixin
from .embed import CaptchaEmbedMixin

from modules.core.moderator_bot import ModeratorBot

_logger = logging.getLogger(__name__)

VERIFICATION_LOCALE = locale_namespace("cogs", "captcha")
VERIFICATION_META = VERIFICATION_LOCALE.child("meta")


class VerificationCog(CaptchaEmbedMixin, CaptchaDeliveryMixin, commands.Cog):
    """Verification flow combining captcha and VPN screening for new members."""

    verification_group = app_commands.Group(
        name="verification",
        description=VERIFICATION_META.string("group_description"),
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: ModeratorBot):
        super().__init__()
        self.bot = bot
        self._session_store = CaptchaSessionStore()
        self._api_base = resolve_api_base()
        self._api_client = CaptchaApiClient(
            self._api_base, os.getenv("CAPTCHA_API_TOKEN")
        )
        self._callback_processor = CaptchaCallbackProcessor(bot, self._session_store)
        self._stream_config = CaptchaStreamConfig.from_env()
        self._stream_listener = CaptchaStreamListener(
            bot,
            self._stream_config,
            self._session_store,
            self._handle_stream_setting_update,
        )
        self._public_verify_url = resolve_public_verify_url()
        self._settings_listener_registered = False
        self._embed_sync_task: asyncio.Task[None] | None = None
        self._stream_start_task: asyncio.Task[None] | None = None
        self._expiry_tasks: dict[tuple[int, int], asyncio.Task[None]] = {}
        self._config_cache: dict[int, CaptchaGuildConfig] = {}
        self._config_cache_expiry: dict[int, float] = {}
        self._config_cache_ttl = 300.0

        if not self._api_client.is_configured:
            _logger.warning(
                "CAPTCHA_API_TOKEN or API base URL missing; captcha verification will be disabled."
            )

    @verification_group.command(
        name="sync",
        description=VERIFICATION_META.string("sync", "description"),
    )
    async def sync_embed_command(self, interaction: Interaction) -> None:
        guild_id = interaction.guild.id
        common_texts = self.bot.translate("cogs.captcha.common",
                                           guild_id=guild_id)
        sync_texts = self.bot.translate("cogs.captcha.sync",
                                         guild_id=guild_id)
        if interaction.guild is None:
            await interaction.response.send_message(
                common_texts["guild_only"], ephemeral=True
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
                sync_texts["error"],
                ephemeral=True,
            )
            return

        if updated:
            await interaction.followup.send(
                sync_texts["updated"],
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                sync_texts["missing_delivery"],
                ephemeral=True,
            )

    @verification_group.command(
        name="request",
        description=VERIFICATION_META.string("request", "description"),
    )
    @app_commands.describe(
        member=VERIFICATION_META.string("request", "member")
    )
    async def request_verification_command(
        self, interaction: Interaction, member: discord.Member
    ) -> None:
        guild_id = interaction.guild.id
        common_texts = self.bot.translate("cogs.captcha.common",
                                           guild_id=guild_id)
        request_texts = self.bot.translate("cogs.captcha.request",
                                           guild_id=guild_id)
        if interaction.guild is None:
            await interaction.response.send_message(
                common_texts["guild_only"], ephemeral=True
            )
            return

        if member.guild is None or member.guild.id != interaction.guild.id:
            await interaction.response.send_message(
                common_texts["wrong_guild"], ephemeral=True
            )
            return

        if member.bot:
            await interaction.response.send_message(
                common_texts["is_bot"], ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        success, message = await self._initiate_verification(member)
        if success:
            await interaction.followup.send(
                message or request_texts["success"], ephemeral=True
            )
            return

        await interaction.followup.send(
            message or request_texts["failure"], ephemeral=True
        )

    async def cog_load(self) -> None:
        if self._stream_start_task is None or self._stream_start_task.done():
            try:
                task = asyncio.create_task(
                    self._start_stream_listener(),
                    name="captcha-stream-start",
                )
            except TypeError:
                task = asyncio.create_task(self._start_stream_listener())
            self._stream_start_task = task
            task.add_done_callback(
                lambda t, *, owner=self: setattr(owner, "_stream_start_task", None)
                if owner._stream_start_task is t
                else None
            )

        if not self._settings_listener_registered:
            mysql.add_settings_listener(self._handle_setting_update)
            self._settings_listener_registered = True

        if self._embed_sync_task is None:
            self._embed_sync_task = asyncio.create_task(self._initial_sync_embeds())

    async def cog_unload(self) -> None:
        if self._stream_start_task is not None:
            self._stream_start_task.cancel()
            try:
                await self._stream_start_task
            except asyncio.CancelledError:
                pass
            finally:
                self._stream_start_task = None

        await self._stream_listener.stop()
        await self._api_client.close()
        if self._settings_listener_registered:
            mysql.remove_settings_listener(self._handle_setting_update)
            self._settings_listener_registered = False

    async def _start_stream_listener(self) -> None:
        try:
            started = await self._stream_listener.start()
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception("Failed to start captcha Redis stream listener")
            print(
                "[VERIFICATION] Failed to start verification Redis stream listener; see logs for details."
            )
            return

        if started:
            print(
                "[VERIFICATION] Redis stream listener subscribed to "
                f"{self._stream_config.stream} as "
                f"{self._stream_config.group}/{self._stream_config.consumer_name}"
                f" (start={self._stream_config.start_id})"
            )
        else:
            print(
                "[VERIFICATION] Verification Redis stream listener disabled; callbacks will not be processed."
            )

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
        for task in list(self._expiry_tasks.values()):
            task.cancel()
        for task in list(self._expiry_tasks.values()):
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._expiry_tasks.clear()

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot or member.guild is None:
            return

        await self._initiate_verification(member)

    async def _initiate_verification(
        self, member: discord.Member
    ) -> tuple[bool, str | None]:
        gid = member.guild.id
        texts = self.bot.translate("cogs.captcha.initiate",
                                   guild_id=gid)
        if member.guild is None:
            return False, texts["not_in_guild"]

        settings = await mysql.get_settings(
            member.guild.id,
            [
                "captcha-verification-enabled",
                "captcha-grace-period",
                "captcha-max-attempts",
                "pre-captcha-roles",
                "captcha-delivery-method",
                "captcha-embed-channel-id",
                "vpn-pre-actions",
            ],
        )

        if not settings.get("captcha-verification-enabled"):
            return False, texts["not_enabled"]

        if not self._api_client.is_configured:
            _logger.info(
                "Captcha API not configured; skipping verification for guild %s", member.guild.id
            )
            return False, texts["not_configured"]

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
                await member.add_roles(
                    *pre_roles,
                    reason=self.bot.translate(
                        "cogs.captcha.roles.assign_reason",
                        guild_id=gid,
                    ),
                )
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

        vpn_pre_actions = parse_role_actions(settings.get("vpn-pre-actions"))
        if vpn_pre_actions:
            reason = self.bot.translate(
                "cogs.captcha.roles.vpn_pre_reason",
                guild_id=gid,
            )
            applied = await apply_role_actions(
                member,
                vpn_pre_actions,
                reason=reason,
                logger=_logger,
            )
            if applied:
                _logger.info(
                    "Applied VPN pre-actions for guild %s user %s: %s",
                    member.guild.id,
                    member.id,
                    ", ".join(applied),
                )

        delivery_method = str(settings.get("captcha-delivery-method") or "dm").lower()
        embed_channel_id = self._coerce_positive_int(settings.get("captcha-embed-channel-id"))
        grace_setting = self._coerce_grace_period(settings.get("captcha-grace-period"))
        grace_display: str | None = None

        unlimited_grace = False
        if grace_setting is not None:
            try:
                if float(grace_setting) <= 0:
                    unlimited_grace = True
            except ValueError:
                pass

        grace_delta: timedelta | None
        if unlimited_grace:
            grace_delta = None
        else:
            parsed_grace = parse_duration(grace_setting) if grace_setting else None
            if parsed_grace is None:
                if grace_setting:
                    _logger.info("Invalid captcha grace period %r for guild %s; using default window.",
                                  grace_setting,
                                  member.guild.id,)
                grace_delta = timedelta(minutes=10)
                grace_display = self._format_duration(grace_delta)
            elif parsed_grace.total_seconds() <= 0:
                grace_delta = None
            else:
                grace_delta = parsed_grace
                grace_display = grace_setting or self._format_duration(parsed_grace)

        max_attempts = self._coerce_positive_int(settings.get("captcha-max-attempts"))

        if delivery_method == "embed" and embed_channel_id:
            session = await self._handle_embed_delivery(
                member,
                embed_channel_id,
                grace_delta,
                grace_display,
                max_attempts,
            )
            if session is not None:
                self._schedule_session_timeout(
                    member.guild.id,
                    member.id,
                    session.expires_at,
                )
                return True, texts["embed_success"]

        start_response = await self._handle_dm_delivery(
            member,
            max_attempts,
            grace_delta,
            grace_display,
        )
        if start_response is not None:
            self._schedule_session_timeout(
                member.guild.id,
                member.id,
                start_response.expires_at if grace_delta is not None else None,
            )
            return True, texts["dm_success"]

        return False, texts["failure"]
    
    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if member.guild is None:
            return
        await self._session_store.remove(member.guild.id, member.id)

    def _schedule_session_timeout(
        self,
        guild_id: int,
        user_id: int,
        expires_at: datetime | None,
    ) -> None:
        if expires_at is None:
            return
        delay = self._calculate_delay_seconds(expires_at)
        if delay is None:
            return
        key = (guild_id, user_id)
        existing = self._expiry_tasks.pop(key, None)
        if existing is not None:
            existing.cancel()
        task = asyncio.create_task(self._run_timeout_task(guild_id, user_id, delay))
        self._expiry_tasks[key] = task
        task.add_done_callback(lambda t, *, _key=key: self._expiry_tasks.pop(_key, None))

    def _calculate_delay_seconds(self, expires_at: datetime | None) -> float | None:
        if expires_at is None:
            return None
        if expires_at.tzinfo is None:
            expiry = expires_at.replace(tzinfo=timezone.utc)
        else:
            expiry = expires_at.astimezone(timezone.utc)
        remaining = (expiry - utcnow()).total_seconds()
        return 0.0 if remaining <= 0 else remaining

    async def _run_timeout_task(
        self, guild_id: int, user_id: int, delay: float
    ) -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            await self._handle_grace_timeout(guild_id, user_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception(
                "Unexpected error enforcing captcha grace period for guild %s user %s",
                guild_id,
                user_id,
            )

    async def _handle_grace_timeout(self, guild_id: int, user_id: int) -> None:
        session = await self._session_store.peek(guild_id, user_id)
        if session is None:
            return
        if not session.is_expired():
            remaining = self._calculate_delay_seconds(session.expires_at)
            if remaining is not None:
                self._schedule_session_timeout(guild_id, user_id, session.expires_at)
            return

        session.expires_at = utcnow() + timedelta(seconds=1)
        payload = CaptchaCallbackPayload(
            guild_id=guild_id,
            user_id=user_id,
            token=session.token or f"expired:{guild_id}:{user_id}",
            status="expired",
            success=False,
            state=session.state,
            failure_reason=self.bot.translate("cogs.captcha.timeout.failure_reason",
                                              guild_id=guild_id),
            metadata={"timeout": True, "reason": "expired"},
        )

        try:
            await self._callback_processor.process(payload)
        except CaptchaProcessingError as exc:
            _logger.info(
                "Failed to apply captcha timeout for guild %s user %s: %s",
                guild_id,
                user_id,
                exc,
            )
        except Exception:
            _logger.exception(
                "Unhandled error while applying captcha timeout for guild %s user %s",
                guild_id,
                user_id,
            )

async def setup_verification(bot: commands.Bot) -> None:
    await bot.add_cog(VerificationCog(bot))