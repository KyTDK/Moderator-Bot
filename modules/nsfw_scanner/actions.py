import os
from typing import Optional

import discord
from discord import Member
from discord.ext import commands
from discord.utils import escape_markdown

from modules.nsfw_scanner.settings_keys import NSFW_ACTION_SETTING
from modules.moderation import strike
from modules.utils import mod_logging, mysql
from modules.utils.localization import TranslateFn, localize_message

from .utils.file_ops import safe_delete

BASE_KEY = "modules.nsfw_scanner.actions"
CONFIDENCE_BASE = "modules.nsfw_scanner.shared.confidence"


def _resolve_translator(bot: commands.Bot) -> TranslateFn | None:
    translate = getattr(bot, "translate", None)
    return translate if callable(translate) else None

async def handle_nsfw_content(
    user: Member,
    bot: commands.Bot,
    guild_id: int,
    reason: str,
    image: Optional[discord.File],
    message: discord.Message,
    confidence: float | None = None,
    confidence_source: str | None = None,
    action_setting: str = NSFW_ACTION_SETTING,
    send_embed: bool = True,
):
    action_flag = await mysql.get_settings(guild_id, action_setting)
    if action_flag:
        try:
            await strike.perform_disciplinary_action(
                user=user,
                bot=bot,
                action_string=action_flag,
                reason=reason,
                source="nsfw",
                message=message
            )
        except Exception:
            pass

    translator = _resolve_translator(bot)
    embed = None
    if send_embed:
        embed = discord.Embed(
            title=localize_message(
                translator,
                BASE_KEY,
                "embed.title",
                fallback="NSFW Content Detected",
                guild_id=guild_id,
            ),
            description=localize_message(
                translator,
                BASE_KEY,
                "embed.description",
                placeholders={
                    "user_mention": user.mention,
                    "user_display": user.display_name,
                    "reason": reason,
                },
                fallback=(
                    "**User:** {user_mention} ({user_display})\n"
                    "**Reason:** {reason}"
                ),
                guild_id=guild_id,
            ),
            color=discord.Color.red(),
        )
        if confidence is not None:
            source_suffix = ""
            if confidence_source:
                source_label = localize_message(
                    translator,
                    CONFIDENCE_BASE,
                    f"sources.{confidence_source}",
                    fallback=confidence_source.replace("_", " ").title(),
                    guild_id=guild_id,
                )
                source_suffix = localize_message(
                    translator,
                    CONFIDENCE_BASE,
                    "source_suffix",
                    placeholders={"source_label": source_label},
                    fallback=" ({source_label})",
                    guild_id=guild_id,
                )
            embed.add_field(
                name=localize_message(
                    translator,
                    CONFIDENCE_BASE,
                    "name",
                    fallback="Confidence",
                    guild_id=guild_id,
                ),
                value=localize_message(
                    translator,
                    CONFIDENCE_BASE,
                    "value",
                    placeholders={
                        "value": f"{confidence:.2f}",
                        "source_suffix": source_suffix,
                    },
                    fallback="{value}{source_suffix}",
                    guild_id=guild_id,
                ),
                inline=False,
            )
        embed.set_thumbnail(url=user.display_avatar.url)

        message_content = getattr(message, "content", "") or ""
        if message_content.strip():
            snippet = message_content.strip()
            if len(snippet) > 512:
                snippet = snippet[:512].rstrip() + "..."
            snippet = escape_markdown(snippet)
            embed.add_field(
                name=localize_message(
                    translator,
                    BASE_KEY,
                    "embed.message_content",
                    fallback="Detected Content",
                    guild_id=guild_id,
                ),
                value=f"```{snippet}```",
                inline=False,
            )

        if image is not None:
            embed.set_image(url=f"attachment://{image.filename}")
        embed.set_footer(
            text=localize_message(
                translator,
                BASE_KEY,
                "embed.footer",
                placeholders={"user_id": user.id},
                fallback="User ID: {user_id}",
                guild_id=guild_id,
            )
        )

    nsfw_channel_id = await mysql.get_settings(user.guild.id, "nsfw-channel")
    strike_channel_id = await mysql.get_settings(user.guild.id, "strike-channel")

    if send_embed and embed is not None:
        if nsfw_channel_id:
            await mod_logging.log_to_channel(embed, nsfw_channel_id, bot, image)
        elif strike_channel_id:
            await mod_logging.log_to_channel(embed, strike_channel_id, bot)

    if image is not None:
        try:
            image.close()
            file_obj = getattr(image, "fp", None)
            file_path = getattr(file_obj, "name", None)
            if isinstance(file_path, str) and os.path.exists(file_path):
                safe_delete(file_path)
        except Exception as e:
            print(f"[cleanup] couldn't delete evidence file: {e}")
