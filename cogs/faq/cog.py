from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord
from discord import Color, Embed, Interaction, app_commands
from discord.ext import commands

from modules.config.premium_plans import PLAN_DISPLAY_NAMES
from modules.core.moderator_bot import ModeratorBot
from modules.faq.config import FAQStreamConfig
from modules.faq.models import FAQEntry, FAQSearchResult
from modules.faq import vector_store
from modules.faq.service import (
    FAQEntryNotFoundError,
    FAQLimitError,
    FAQServiceError,
    add_faq_entry,
    delete_faq_entry,
    find_best_faq_answer,
    list_faq_entries,
)
from modules.faq.settings_keys import FAQ_ENABLED_SETTING, FAQ_THRESHOLD_SETTING
from modules.faq.stream import FAQStreamProcessor
from modules.i18n.strings import locale_namespace
from modules.utils import mod_logging, mysql
from modules.utils.interaction_responses import send_ephemeral_response
from modules.utils.localization import LocalizedError
from modules.utils.log_channel import DeveloperLogField, log_to_developer_channel

LOCALE = locale_namespace("cogs", "faq")
META = LOCALE.child("meta")
ADD_LOCALE = META.child("add")
REMOVE_LOCALE = META.child("remove")
LIST_LOCALE = META.child("list")
ENABLE_LOCALE = META.child("enable")
RESPONSE_LOCALE = LOCALE.child("response")

log = logging.getLogger(__name__)


def _parse_bool_setting(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def _trim_field_value(value: str, limit: int = 1024) -> str:
    if len(value) <= limit:
        return value
    truncated = value[: limit - 3].rstrip()
    return f"{truncated}..."


class FAQCog(commands.Cog):
    """Slash commands and message handler for FAQ responses."""

    def __init__(self, bot: ModeratorBot) -> None:
        self.bot = bot
        self._stream_config = FAQStreamConfig.from_env()
        self._stream_processor = FAQStreamProcessor(bot, self._stream_config)
        self._stream_start_task: asyncio.Task[None] | None = None

    faq_group = app_commands.Group(
        name="faq",
        description=META.string("group_description"),
        default_permissions=discord.Permissions(manage_messages=True),
        guild_only=True,
    )

    @faq_group.command(
        name="add",
        description=ADD_LOCALE.string("description"),
    )
    @app_commands.describe(
        question=ADD_LOCALE.child("params").string("question"),
        answer=ADD_LOCALE.child("params").string("answer"),
    )
    async def add_faq(
        self,
        interaction: Interaction,
        question: str,
        answer: str,
    ) -> None:
        if not interaction.guild:
            await send_ephemeral_response(interaction, content="Guild context required.")
            return

        guild_id = interaction.guild.id
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            entry = await add_faq_entry(guild_id, question, answer)
        except FAQLimitError as exc:
            plan_name = PLAN_DISPLAY_NAMES.get(exc.plan, exc.plan.title())
            message = self.bot.translate(
                "cogs.faq.add.limit_reached",
                guild_id=guild_id,
                placeholders={
                    "limit": exc.limit,
                    "plan": plan_name,
                },
            )
            await interaction.followup.send(message, ephemeral=True)
            return
        except FAQServiceError as exc:
            message = self.bot.translate(
                "cogs.faq.add.failure",
                guild_id=guild_id,
                placeholders={"reason": str(exc)},
            )
            await interaction.followup.send(message, ephemeral=True)
            return

        message = self.bot.translate(
            "cogs.faq.add.success",
            guild_id=guild_id,
            placeholders={"entry_id": entry.entry_id},
        )
        await interaction.followup.send(message, ephemeral=True)

    @faq_group.command(
        name="remove",
        description=REMOVE_LOCALE.string("description"),
    )
    @app_commands.describe(
        entry_id=REMOVE_LOCALE.child("params").string("entry_id"),
    )
    async def remove_faq(self, interaction: Interaction, entry_id: int) -> None:
        if not interaction.guild:
            await send_ephemeral_response(interaction, content="Guild context required.")
            return

        guild_id = interaction.guild.id
        await interaction.response.defer(ephemeral=True)
        try:
            entry = await delete_faq_entry(guild_id, entry_id)
        except FAQEntryNotFoundError:
            message = self.bot.translate(
                "cogs.faq.remove.not_found",
                guild_id=guild_id,
                placeholders={"entry_id": entry_id},
            )
            await interaction.followup.send(message, ephemeral=True)
            return
        except FAQServiceError as exc:
            message = self.bot.translate(
                "cogs.faq.remove.failure",
                guild_id=guild_id,
                placeholders={"reason": str(exc)},
            )
            await interaction.followup.send(message, ephemeral=True)
            return

        message = self.bot.translate(
            "cogs.faq.remove.success",
            guild_id=guild_id,
            placeholders={"entry_id": entry.entry_id},
        )
        await interaction.followup.send(message, ephemeral=True)

    @faq_group.command(
        name="list",
        description=LIST_LOCALE.string("description"),
    )
    async def list_faq(self, interaction: Interaction) -> None:
        if not interaction.guild:
            await send_ephemeral_response(interaction, content="Guild context required.")
            return

        guild_id = interaction.guild.id
        entries = await list_faq_entries(guild_id)
        if not entries:
            message = self.bot.translate(
                "cogs.faq.list.empty",
                guild_id=guild_id,
            )
            await interaction.response.send_message(message, ephemeral=True)
            return

        embed = self._build_faq_list_embed(guild_id, entries)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    def _build_faq_list_embed(self, guild_id: int, entries: list[FAQEntry]) -> Embed:
        title = self.bot.translate("cogs.faq.list.embed_title", guild_id=guild_id)
        description = self.bot.translate("cogs.faq.list.embed_description", guild_id=guild_id)
        embed = Embed(title=title, description=description, color=Color.blurple())
        for entry in entries[:25]:
            question_label = self.bot.translate(
                "cogs.faq.list.question_label",
                guild_id=guild_id,
                placeholders={"entry_id": entry.entry_id},
            )
            field_value = self.bot.translate(
                "cogs.faq.list.entry_value",
                guild_id=guild_id,
                placeholders={
                    "question": _trim_field_value(entry.question, limit=256),
                    "answer": _trim_field_value(entry.answer),
                },
            )
            embed.add_field(
                name=question_label,
                value=field_value,
                inline=False,
            )
        if len(entries) > 25:
            overflow = self.bot.translate(
                "cogs.faq.list.overflow",
                guild_id=guild_id,
                placeholders={"remaining": len(entries) - 25},
            )
            embed.set_footer(text=overflow)
        return embed

    @faq_group.command(
        name="enable",
        description=ENABLE_LOCALE.string("description"),
    )
    @app_commands.describe(
        enabled=ENABLE_LOCALE.child("params").string("enabled"),
    )
    async def set_faq_enabled(self, interaction: Interaction, enabled: bool) -> None:
        if not interaction.guild:
            await send_ephemeral_response(interaction, content="Guild context required.")
            return

        guild_id = interaction.guild.id
        previous_value = await mysql.get_settings(guild_id, FAQ_ENABLED_SETTING)
        previous_enabled = _parse_bool_setting(previous_value, default=False)

        if previous_enabled == enabled:
            message_key = "already_enabled" if enabled else "already_disabled"
            message = self.bot.translate(
                f"cogs.faq.enable.{message_key}",
                guild_id=guild_id,
            )
            await interaction.response.send_message(message, ephemeral=True)
            return

        try:
            await mysql.update_settings(guild_id, FAQ_ENABLED_SETTING, enabled)
        except LocalizedError as exc:
            message = exc.localize(self.bot.translate, guild_id=guild_id)
            await interaction.response.send_message(message, ephemeral=True)
            return
        except Exception as exc:
            message = self.bot.translate(
                "cogs.faq.enable.failure",
                guild_id=guild_id,
                placeholders={"reason": str(exc)},
            )
            await interaction.response.send_message(message, ephemeral=True)
            return

        message_key = "success_enabled" if enabled else "success_disabled"
        message = self.bot.translate(
            f"cogs.faq.enable.{message_key}",
            guild_id=guild_id,
        )
        await interaction.response.send_message(message, ephemeral=True)

    async def handle_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return

        guild_id = message.guild.id
        faq_settings = await mysql.get_settings(
            guild_id,
            [FAQ_ENABLED_SETTING, FAQ_THRESHOLD_SETTING],
        )

        enabled_value = (
            faq_settings.get(FAQ_ENABLED_SETTING)
            if isinstance(faq_settings, dict)
            else faq_settings
        )
        enabled = _parse_bool_setting(enabled_value, default=False)
        if not enabled:
            return

        threshold = None
        if isinstance(faq_settings, dict):
            threshold = faq_settings.get(FAQ_THRESHOLD_SETTING)

        try:
            result = await find_best_faq_answer(
                guild_id,
                message.content,
                threshold=threshold,
            )
        except Exception as exc:  # pragma: no cover - defensive path
            log.exception(
                "FAQ auto-response failed for guild_id=%s channel_id=%s message_id=%s",
                guild_id,
                getattr(message.channel, "id", "unknown"),
                message.id,
            )
            threshold_label = str(threshold) if threshold is not None else "<default>"
            content_preview = _trim_field_value(message.content or "", limit=512) or "(empty)"
            await log_to_developer_channel(
                self.bot,
                summary="FAQ responder error",
                severity="error",
                description=f"{exc.__class__.__name__}: {exc}",
                fields=[
                    DeveloperLogField("Guild", f"{guild_id}"),
                    DeveloperLogField("Channel", f"{getattr(message.channel, 'id', 'unknown')}"),
                    DeveloperLogField("Message", f"{message.id}"),
                    DeveloperLogField("Author", f"{message.author} ({message.author.id})"),
                    DeveloperLogField("Threshold", threshold_label),
                    DeveloperLogField("Content", content_preview, inline=False),
                ],
            )
            return

        if result is None:
            return

        if result.used_fallback:
            threshold_label = str(threshold) if threshold is not None else "<default>"
            content_preview = _trim_field_value(message.content or "", limit=256) or "(empty)"
            vector_status = vector_store.get_debug_info()
            vector_status_lines = [
                f"available={vector_status.get('collection_ready')}",
                f"fallback_active={vector_status.get('fallback_active')}",
                f"init_started={vector_status.get('init_started')}",
                f"ready_event={vector_status.get('ready_event')}",
                f"milvus_dependency={vector_status.get('milvus_dependency')}",
                f"endpoint={vector_status.get('host')}:{vector_status.get('port')}",
            ]
            last_error = vector_status.get("last_error")
            if last_error:
                vector_status_lines.append(f"last_error={last_error}")
            vector_status_field = "\n".join(vector_status_lines)
            await log_to_developer_channel(
                self.bot,
                summary="FAQ fallback matcher engaged",
                severity="warning",
                description="Vector store unavailable; served FAQ response via string similarity fallback.",
                fields=[
                    DeveloperLogField("Guild", f"{guild_id}"),
                    DeveloperLogField("Channel", f"{getattr(message.channel, 'id', 'unknown')}"),
                    DeveloperLogField("Message", f"{message.id}"),
                    DeveloperLogField("Author", f"{message.author} ({message.author.id})"),
                    DeveloperLogField("Threshold", threshold_label),
                    DeveloperLogField("Similarity", f"{result.similarity:.3f}"),
                    DeveloperLogField(
                        "Matched FAQ",
                        f"#{result.entry.entry_id}: {_trim_field_value(result.entry.question, limit=128)}",
                        inline=False,
                    ),
                    DeveloperLogField("Content", content_preview, inline=False),
                    DeveloperLogField("Vector store", vector_status_field, inline=False),
                ],
            )

        embed = self._build_response_embed(guild_id, message.author, result)
        try:
            await mod_logging.log_to_channel(embed, message.channel.id, self.bot)
        except discord.Forbidden:
            return

    def _build_response_embed(
        self,
        guild_id: int,
        author: discord.abc.User,
        result: FAQSearchResult,
    ) -> Embed:
        entry = result.entry
        similarity_percent = round(result.similarity * 100)

        title = self.bot.translate("cogs.faq.response.title", guild_id=guild_id)
        description = self.bot.translate(
            "cogs.faq.response.description",
            guild_id=guild_id,
            placeholders={
                "user": author.mention,
                "confidence": similarity_percent,
            },
        )

        embed = Embed(title=title, description=description, color=Color.green())

        question_label = self.bot.translate("cogs.faq.response.question_label", guild_id=guild_id)
        answer_label = self.bot.translate("cogs.faq.response.answer_label", guild_id=guild_id)

        embed.add_field(
            name=f"{question_label} (#{entry.entry_id})",
            value=_trim_field_value(entry.question),
            inline=False,
        )
        embed.add_field(
            name=answer_label,
            value=_trim_field_value(entry.answer),
            inline=False,
        )
        return embed

    async def cog_load(self) -> None:
        if self._stream_start_task is None or self._stream_start_task.done():
            task = asyncio.create_task(self._ensure_stream_started(), name="faq-stream-start")
            self._stream_start_task = task
            task.add_done_callback(
                lambda t, owner=self: setattr(owner, "_stream_start_task", None)
                if owner._stream_start_task is t
                else None
            )

    async def cog_unload(self) -> None:
        if self._stream_start_task is not None:
            self._stream_start_task.cancel()
            try:
                await self._stream_start_task
            except asyncio.CancelledError:
                pass
            finally:
                self._stream_start_task = None

        await self._stream_processor.stop()

    async def _ensure_stream_started(self) -> None:
        if not self._stream_config.enabled:
            return

        await self.bot.wait_until_ready()
        wait_mysql = getattr(self.bot, "_wait_for_mysql_ready", None)
        if callable(wait_mysql):
            try:
                await wait_mysql()
            except Exception:
                logging.getLogger(__name__).exception("Failed waiting for MySQL readiness before starting FAQ stream")
                return

        try:
            await self._stream_processor.start()
        except Exception:
            logging.getLogger(__name__).exception("Failed to start FAQ Redis stream processor")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FAQCog(bot))
