import discord
from modules.utils import mysql
import os
from dotenv import load_dotenv
from discord.ext import commands
from discord import Interaction, app_commands
from discord.app_commands import check

load_dotenv()
MYSQL_HEISTBOT_PASSWORD = os.getenv('MYSQL_HEISTBOT_PASSWORD')
MYSQL_HEISTBOT_USER = os.getenv('MYSQL_HEISTBOT_USER')
MYSQL_HEISTBOT_DATABASE = os.getenv('MYSQL_HEISTBOT_DATABASE')

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


async def message_user(user: discord.User, content: str, bot:commands.Bot, guild: discord.Guild, embed: discord.Embed = None):
    try:
        # Attempt to send a DM
        message = await user.send(content, embed=embed) if embed else await user.send(content)
        
        return message
    except discord.errors.Forbidden:  # If DMs are blocked
        # Check if the user has a private channel stored in the database
        private_channel_id, _ = mysql.execute_query("SELECT channel_id FROM private_channels WHERE user_id = %s", (user.id,), fetch_one=True, 
                                                 database=MYSQL_HEISTBOT_DATABASE,
                                                 user=MYSQL_HEISTBOT_USER,
                                                 password=MYSQL_HEISTBOT_PASSWORD)
        if private_channel_id:
            private_channel = await bot.fetch_channel(int(private_channel_id[0]))  # Fetch the channel
            if private_channel:
                message = await private_channel.send(f"{user.mention}, {content}", embed=embed) if embed else await private_channel.send(f"{user.mention}, {content}")
                return message
        else:
            # If no private channel exists, create one and store it
            if guild:
                category = discord.utils.get(guild.categories, name="Private")
                if not category:
                    category = await guild.create_category("Private", 
                        position=0, 
                        overwrites={
                        guild.default_role: discord.PermissionOverwrite(view_channel=False)
                    })
                private_channel = await guild.create_text_channel(
                    name=f"private-{user.name}",
                    topic=f"Private messages with {user.name}",
                    category=category,
                    position=0,
                    overwrites={
                        guild.default_role: discord.PermissionOverwrite(view_channel=False),  # Hide from everyone
                        user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),  # Allow user
                        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=True)  # Allow bot
                    },
                    reason=f"Private channel for {user.name}"
                )
                await private_channel.edit(position=0)
                # Corrected order: channel id then user id
                mysql.execute_query("INSERT INTO private_channels (channel_id, user_id) VALUES (%s, %s)", (private_channel.id, user.id), 
                                                 database=MYSQL_HEISTBOT_DATABASE,
                                                 user=MYSQL_HEISTBOT_USER,
                                                 password=MYSQL_HEISTBOT_PASSWORD)
                message = await private_channel.send(f"{user.mention}, {content}", embed=embed) if embed else await private_channel.send(f"{user.mention}, {content}")
                return message
            else:
                print("Failed to get guild")