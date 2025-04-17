from discord.ext import commands
from discord import app_commands, Interaction, Member, Embed, Color
from modules.utils.mysql import execute_query
from modules.utils.user_utils import has_role_or_permission
from discord.app_commands.errors import MissingPermissions
from modules.moderation import strike
import discord


class moderation(commands.Cog):
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
    @app_commands.default_permissions(moderate_members=True)
    async def strike(self, interaction: Interaction, user: Member, reason: str):
        """strike a specific user."""
        if await strike.strike(user=user, bot=self.bot, reason=reason, interaction=interaction):
            # Log the strike in the current channel
            log_embed = Embed(
                title="User Strike",
                description=f"{user.display_name} has received a strike.",
                color=Color.red()
            )
            log_embed.add_field(name="Reason", value=reason, inline=False)
            log_embed.add_field(name="Strike by", value=interaction.user.mention, inline=False)
            log_embed.set_thumbnail(url=user.display_avatar.url)
            log_embed.timestamp = interaction.created_at
            await interaction.followup.send(embed=log_embed, ephemeral=True)
        else:
            await interaction.followup.send("An error occured, please try again")
    
    # Get strikes
    @strike_group.command(
        name="get",
        description="Get strikes of a specific user."
    )
    async def get_strikes(self, interaction: Interaction, user: Member):
        """Retrieve strikes for a specified user."""
        # Defer the response to acknowledge the interaction
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild.id
        # Retrieve strikes from the database.
        # Adjust the SELECT query as needed for your database schema.
        strikes, _ = execute_query(
            "SELECT reason, striked_by_id, timestamp FROM strikes WHERE guild_id = %s AND user_id = %s ORDER BY timestamp DESC",
            (guild_id, user.id),  # Passing both guild_id and user_id as parameters
            fetch_all=True
        )

        # Create an embed to display the strikes
        embed = Embed(
            title=f"Strikes for {user.display_name}",
            color=Color.red()
        )
        if not strikes:
            embed.description = "No strikes found for this user."
        else:
            for strike in strikes:
                reason, striked_by_id, timestamp = strike
                # Retrieve the member who issued the strike (if possible)
                strike_by = interaction.guild.get_member(striked_by_id)
                strike_by_name = strike_by.display_name if strike_by else "Unknown"
                embed.add_field(
                    name=f"Strike by: {strike_by_name}",
                    value=f"Reason: {reason}\nTime: {timestamp}",
                    inline=False
                )
        await interaction.followup.send(embed=embed)
    
    #Clear strikes
    @strike_group.command(
        name="clear",
        description="Clear all strikes of a specific user."
    )
    async def clear_strikes(self, interaction: Interaction, user: Member):
        """Clear all strikes for a specified user."""
        # Defer the response to acknowledge the interaction
        await interaction.response.defer(ephemeral=True)

        # Delete strikes from the database
        _, rows_affected = execute_query(
            "DELETE FROM strikes WHERE user_id = %s AND guild_id = %s",
            (user.id, interaction.guild.id)  # Passing both guild_id and user_id as parameters
        )

        # Provide feedback
        if rows_affected > 0:
            message = f"Successfully cleared {rows_affected} strike(s) for {user.mention}."
        else:
            message = f"No strikes found for {user.mention}."

        await interaction.followup.send(message, ephemeral=True)
    
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
    await bot.add_cog(moderation(bot))
