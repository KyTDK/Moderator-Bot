from datetime import timezone
from typing import Optional
from discord.ext import commands
from discord import app_commands, Interaction, Member, Embed, Color
from modules.moderation import strike
import discord
import io
from discord import File
from modules.utils import mysql
from modules.utils.actions import VALID_ACTION_VALUES, action_choices
from modules.utils.discord_utils import safe_get_user
from modules.utils.strike import validate_action
from modules.variables.TimeString import TimeString

async def autocomplete_strike_action(interaction: Interaction, current: str) -> list[app_commands.Choice[str]]:
    settings = await mysql.get_settings(interaction.guild.id, "strike-actions") or {}
    all_actions = set()
    for action_list in settings.values():
        all_actions.update(action_list)
    return [
        app_commands.Choice(name=action, value=action)
        for action in sorted(all_actions)
        if current.lower() in action.lower()
    ][:25]

class StrikesCog(commands.Cog):
    """A cog for moderation commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    strike_group = app_commands.Group(
        name="strikes",
        description="Strike management commands.",
        default_permissions=discord.Permissions(moderate_members=True),
        guild_only=True
    )

    #strike
    @app_commands.command(
        name="strike",
        description="Strike a specific user."
    )
    @app_commands.describe(
        user="The member to strike.",
        reason="The reason for the strike.",
        expiry="Optional expiry duration (e.g., 30d, 2w)."
    )
    @app_commands.default_permissions(moderate_members=True)
    async def strike(
        self,
        interaction: Interaction,
        user: Member,
        reason: str,
        expiry: Optional[str] = None
    ):
        """Strike a specific user."""
        try:
            embed = await strike.strike(user=user, bot=self.bot, reason=reason, interaction=interaction, expiry=TimeString(expiry))
        except ValueError as ve:
            await interaction.response.send_message(str(ve), ephemeral=True)
            return

        if embed:
            embed.set_thumbnail(url=user.display_avatar.url)
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.followup.send(
                "An error occurred, please try again. If the issue persists, please join the support server. The link can be found at the bottom of `/help`.",
                ephemeral=True
            )

    @strike_group.command(
        name="get",
        description="Get strikes of a specific user."
    )
    async def get_strikes(self, interaction: Interaction, user: Member):
        """Retrieve strikes for a specified user."""
        await interaction.response.defer(ephemeral=True)

        strikes = await mysql.get_strikes(user.id, interaction.guild.id)

        if not strikes:
            await interaction.followup.send(embed=Embed(
                title=f"Strikes for {user.display_name}",
                description="No strikes found for this user.",
                color=Color.red()
            ))
            return

        entries = []
        for strike_entry in strikes:
            # Unpack the strike details
            strike_id, reason, striked_by_id, timestamp, expires_at = strike_entry

            # Ensure UTC to work properly with discord UNIX embed
            timestamp = timestamp.replace(tzinfo=timezone.utc)
            expires_at = expires_at.replace(tzinfo=timezone.utc) if expires_at else None            

            # Get the name of the user who issued the strike
            strike_by = await safe_get_user(self.bot, striked_by_id)
            strike_by_name = strike_by.display_name if strike_by else "Unknown"

            # Format the expiry time using Discord's dynamic timestamp
            if expires_at:
                expiry_str = f"<t:{int(expires_at.timestamp())}:R>"
            else:
                expiry_str = "Never"

            # Create the entry
            entry = {
                "title": f"Strike ID: {strike_id} | By: {strike_by_name}",
                "value": f"Reason: {reason}\nIssued: <t:{int(timestamp.timestamp())}:R>\nExpires: {expiry_str}"
            }
            entries.append(entry)

        # Build the content string
        content = f"Strikes for {user.display_name}:\n\n"
        for entry in entries:
            content += f"{entry['title']}\n{entry['value']}\n\n"

        # Decide how to deliver the data
        if len(entries) > 25 or len(content) > 6000:
            # Too many fields or the plain-text variant is too long → send a file
            file = File(io.BytesIO(content.encode()), filename=f"{user.name}_strikes.txt")
            await interaction.followup.send(
                content="Strike list is too long to display inline, sending as a file:",
                file=file,
                ephemeral=True,
            )
        else:
            # Safe to use a single embed
            embed = Embed(
                title=f"Strikes for {user.display_name}",
                color=Color.red()
            )
            for entry in entries:
                embed.add_field(
                    name=entry["title"],
                    value=entry["value"],
                    inline=False
                )
            await interaction.followup.send(embed=embed, ephemeral=True)
            
    @strike_group.command(
        name="remove",
        description="Remove a specific strike by its ID."
    )
    @app_commands.describe(
        strike_id="The ID of the strike to remove",
        user="The user who received the strike"
    )
    @app_commands.default_permissions(moderate_members=True)
    async def remove_strike(self, interaction: Interaction, strike_id: int, user: Member):
        """Remove a strike by its unique ID and target user."""
        await interaction.response.defer(ephemeral=True)

        # Ensure the strike matches the guild and the user
        _, rows_affected = await mysql.execute_query(
            "DELETE FROM strikes WHERE id = %s AND guild_id = %s AND user_id = %s",
            (strike_id, interaction.guild.id, user.id)
        )

        if rows_affected > 0:
            message = f"Strike with ID `{strike_id}` for {user.mention} has been successfully removed."
        else:
            message = (
                f"No strike with ID `{strike_id}` found for {user.mention} in this guild.\n"
                f"Use `/strikes get <user>` to display all strikes and their IDs."
            )

        await interaction.followup.send(message, ephemeral=True)

    # Clear strikes
    @strike_group.command(
        name="clear",
        description="Clear all strikes of a specific user."
    )
    async def clear_strikes(self, interaction: Interaction, user: Member):
        """Clear all strikes for a specified user."""
        await interaction.response.defer(ephemeral=True)

        # Delete only active strikes from the database, we keep inactive ones for analytics
        _, rows_affected = await mysql.execute_query(
            """
            DELETE FROM strikes
            WHERE user_id = %s
            AND guild_id = %s
            AND (expires_at IS NULL OR expires_at > UTC_TIMESTAMP())
            """,
            (user.id, interaction.guild.id)
        )

        # Provide feedback
        if rows_affected > 0:
            message = f"Successfully cleared {rows_affected} strike(s) for {user.mention}."
        else:
            message = f"No strikes found for {user.mention}."

        await interaction.followup.send(message, ephemeral=True)

    @strike_group.command(name="add_action", description="Add an additional action for a strike level.")
    @app_commands.describe(
        number_of_strikes="Number of strikes required to trigger the action.",
        action="Action to add.",
        duration="Duration (only for timeout, e.g., 1h, 30m). Leave empty otherwise.",
    )
    @app_commands.choices(action=action_choices(exclude=("delete", "strike")))
    async def add_strike_action(
        self,
        interaction: Interaction,
        number_of_strikes: int,
        action: str,
        duration: str = None,
        role: discord.Role = None,
        reason: str = None,
    ):
        await interaction.response.defer(ephemeral=True)
        strike_actions = await mysql.get_settings(interaction.guild.id, "strike-actions") or {}
        key = str(number_of_strikes)
        action_str = await validate_action(
            interaction=interaction,
            action=action,
            duration=duration,
            role=role,
            valid_actions=VALID_ACTION_VALUES,
            ephemeral=True,
            param=reason,
        )
        if action_str is None:
            return
        actions_list = strike_actions.get(key, [])
        if action_str in actions_list:
            await interaction.followup.send(
                f"Action `{action_str}` already exists for `{key}` strikes.",
                ephemeral=True,
            )
            return
        actions_list.append(action_str)
        strike_actions[key] = actions_list
        await mysql.update_settings(interaction.guild.id, "strike-actions", strike_actions)
        await interaction.followup.send(
            f"Added `{action_str}` to actions for `{key}` strikes.",
            ephemeral=True,
        )

    @strike_group.command(name="remove_action", description="Remove an action from a strike level.")
    @app_commands.describe(
        number_of_strikes="Number of strikes associated with the action.",
        action="Exact action string to remove.",
    )
    @app_commands.autocomplete(action=autocomplete_strike_action)
    async def remove_strike_action(self, interaction: Interaction, number_of_strikes: int, action: str):
        await interaction.response.defer(ephemeral=True)
        strike_actions = await mysql.get_settings(interaction.guild.id, "strike-actions") or {}
        key = str(number_of_strikes)
        actions_list = strike_actions.get(key)
        if not actions_list or action not in actions_list:
            await interaction.followup.send(
                f"Action `{action}` not found for `{key}` strikes.",
                ephemeral=True,
            )
            return
        actions_list.remove(action)
        if actions_list:
            strike_actions[key] = actions_list
        else:
            strike_actions.pop(key)
        await mysql.update_settings(interaction.guild.id, "strike-actions", strike_actions)
        await interaction.followup.send(
            f"Removed `{action}` from `{key}` strike actions.",
            ephemeral=True,
        )

    @strike_group.command(name="view_actions", description="View all configured strike actions.")
    async def view_strike_actions(self, interaction: Interaction):
        strike_actions = await mysql.get_settings(interaction.guild.id, "strike-actions") or {}
        if not strike_actions:
            await interaction.response.send_message("No strike actions configured.", ephemeral=True)
            return
        lines = []
        for k in sorted(strike_actions.keys(), key=int):
            actions = ", ".join(strike_actions[k])
            lines.append(f"{k}: {actions}")
        await interaction.response.send_message(
            "**Current strike actions:**\n" + "\n".join(lines),
            ephemeral=True,
        )

    # Warn channel or optionally user
    @app_commands.command(
        name="intimidate",
        description="Intimidate the channel, or a specific user."
    )
    @app_commands.describe(
        user="The user to intimidate. If not provided, the entire channel will be addressed with a broader message.",
        channel="If true, sends the user warning to the channel; otherwise, sends a direct message to the user."
    )
    @app_commands.default_permissions(moderate_members=True)
    async def intimidate(self, interaction: Interaction, user: Member = None, channel: bool = False):
        """Intimidate the user."""
        # Create an embed with an intimidating message
        if user:
            embed = Embed(
                title=f"⚠️ Final Warning for {user.display_name}",
                description=(
                    f"{user.mention},\n"
                    "Your actions are pushing the limits of what is acceptable within this server. "
                    "Consider this your final warning before a strike is issued against your account. "
                    "Continued disregard for the community guidelines will result in immediate disciplinary action, "
                    "which may include further penalties or removal from the server.\n\n"
                    "This is not a request—comply with the rules now."
                ),
                color=Color.red()
            )
            if channel:
                # Send the embed to the channel
                await interaction.channel.send(embed=embed)
            else:
                # DM the user with the embed
                await user.send(embed=embed)
        else:
            """Intimidate the channel."""
            # Create an embed with an intimidating message
            embed = Embed(
                title="Official Moderation Notice",
                description=(
                    "Please be advised that Moderator Bot is actively monitoring all activity in this channel. "
                    "Any violations of community guidelines may result in disciplinary action. "
                    "The severity and nature of these actions will depend on the circumstances of each case. "
                    "We appreciate your cooperation in helping maintain a respectful and safe environment."
                ),
                color=Color.red()
            )
            # Send the embed to the channel
            await interaction.channel.send(embed=embed)
        await interaction.response.send_message("Sent message.", ephemeral=True) 

async def setup(bot: commands.Bot):
    await bot.add_cog(StrikesCog(bot))
