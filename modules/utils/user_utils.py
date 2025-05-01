import discord
from discord import Interaction, app_commands

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