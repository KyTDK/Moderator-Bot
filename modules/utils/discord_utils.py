import discord
from discord import Interaction, app_commands
from discord.ext import commands
from typing import Optional
from modules import cache
from modules.utils import mysql

def has_roles(*role_names: str):
    async def predicate(interaction: Interaction) -> bool:
        # Check if the user has any of the specified roles by name
        for role_name in role_names:
            if discord.utils.get(interaction.user.roles, name=role_name):
                return True
        return False
    return app_commands.check(predicate)


def has_role_or_permission(*role_names: str):
    async def predicate(interaction: Interaction) -> bool:
        user = interaction.user

        # Check if the user has moderate_members permission
        if user.guild_permissions.moderate_members:
            return True

        # Check if the user has any of the specified roles
        if any(role.name in role_names for role in user.roles):
            return True

        return False

    return app_commands.check(predicate)

async def message_user(user: discord.User, content: str, embed: discord.Embed = None):
    # Attempt to send a DM
    try:
        message = await user.send(content, embed=embed) if embed else await user.send(content)
    except discord.Forbidden:
        # ignore
        message = None
        pass
    return message

async def safe_get_channel(bot: commands.Bot, channel_id: int) -> discord.TextChannel:
    chan = bot.get_channel(channel_id)
    if chan is None:
        try:
            chan = await bot.fetch_channel(channel_id)
        except discord.HTTPException as e:
            print(f"failed to fetch channel {channel_id}: {e}")
    return chan

async def safe_get_user(bot: discord.Client, user_id: int) -> Optional[discord.User]:
    """
    Return a User object without wasting REST quota.
    • First try the local cache (bot.get_user)
    • If missing, fall back to fetch_user (1 REST call)
    • Returns None if the fetch fails (user deleted or we lack perms)
    """
    user = bot.get_user(user_id)
    if user is not None:                       # already in cache
        return user

    try:
        user = await bot.fetch_user(user_id)   # 1 REST call
        return user
    except discord.NotFound:
        # user_id no longer exists (account deleted)
        return None
    except discord.Forbidden:
        # we don’t share a guild and the user has DMs disabled
        return None
    except discord.HTTPException as e:
        # network / rate‑limit issue – log and fail gracefully
        print(f"[safe_get_user] fetch_user({user_id}) failed: {e}")
        return None
    
async def safe_get_member(guild: discord.Guild, user_id: int) -> Optional[discord.Member]:
    """
    Safely get a Member from cache or fetch.
    Returns None if the user is not in the guild or can't be fetched.
    """
    member = guild.get_member(user_id)
    if member is not None:
        return member

    try:
        return await guild.fetch_member(user_id)
    except (discord.NotFound, discord.Forbidden):
        return None
    except discord.HTTPException as e:
        print(f"[safe_get_member] fetch_member({user_id}) failed: {e}")
        return None
    
async def safe_get_message(channel: discord.TextChannel, message_id: int) -> Optional[discord.Message]:
    """
    Safely get a Message from cache or fetch.
    Returns None if the message is not found or can't be fetched.
    """
    message = cache.get_cached_message(channel.guild.id, message_id)
    if message is not None:
        return message
    try:
        message = await channel.fetch_message(message_id)
        cache.cache_message(message)  # Cache the message for future use
        return message
    except (discord.NotFound, discord.Forbidden):
        return None
    except discord.HTTPException as e:
        print(f"[safe_get_message] fetch_message({message_id}) failed: {e}")
        return None
    
async def require_accelerated(interaction: Interaction):
    """
    Check if the command is being used in a server with an Accelerated subscription.
    If not, respond with an error message.
    """
    if not await mysql.is_accelerated(guild_id=interaction.guild.id):
        await interaction.response.send_message(
            "This command is only available for Accelerated (Premium) servers. Use `/accelerated subscribe` to enable it.",
            ephemeral=True
        )
        return False
    return True
    