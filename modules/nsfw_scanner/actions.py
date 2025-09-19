import discord
from discord import Member
from discord.ext import commands

from cogs.nsfw import NSFW_ACTION_SETTING
from modules.moderation import strike
from modules.utils import mod_logging, mysql

from .utils import safe_delete

async def handle_nsfw_content(user: Member, bot: commands.Bot, guild_id: int, reason: str, image: discord.File, message: discord.Message, confidence: float | None = None, confidence_source: str | None = None):
    action_flag = await mysql.get_settings(guild_id, NSFW_ACTION_SETTING)
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

    embed = discord.Embed(
        title="NSFW Content Detected",
        description=(
            f"**User:** {user.mention} ({user.display_name})\n"
            f"**Reason:** {reason}"
        ),
        color=discord.Color.red(),
    )
    if confidence is not None:
        suffix = f" ({confidence_source})" if confidence_source else ""
        embed.add_field(
            name="Confidence",
            value=f"{confidence:.2f}{suffix}",
            inline=False,
        )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_image(url=f"attachment://{image.filename}")
    embed.set_footer(text=f"User ID: {user.id}")

    nsfw_channel_id = await mysql.get_settings(user.guild.id, "nsfw-channel")
    strike_channel_id = await mysql.get_settings(user.guild.id, "strike-channel")

    if nsfw_channel_id:
        await mod_logging.log_to_channel(embed, nsfw_channel_id, bot, image)
    elif strike_channel_id:
        await mod_logging.log_to_channel(embed, strike_channel_id, bot)

    try:
        image.close()
        safe_delete(image.fp.name)
    except Exception as e:
        print(f"[cleanup] couldn't delete evidence file: {e}")
