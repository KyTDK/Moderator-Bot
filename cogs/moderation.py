from typing import Optional
from discord.ext import commands
from discord import app_commands, Interaction, Member, Embed, Color
from modules.moderation import strike
import discord
import io
from discord import File
from modules.utils import mysql
from modules.variables.TimeString import TimeString
from discord.utils import utcnow
from modules.utils.time import parse_duration

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
        expiry_duration = parse_duration(expiry) if expiry else None
        expires_at = utcnow() + expiry_duration if expiry_duration else None

        if await strike.strike(user=user, bot=self.bot, reason=reason, interaction=interaction, expiry=TimeString(expiry)):
            log_embed = Embed(
                title="User Strike",
                description=f"{user.display_name} has received a strike.",
                color=Color.red()
            )
            log_embed.add_field(name="Reason", value=reason, inline=False)
            log_embed.add_field(name="Strike by", value=interaction.user.mention, inline=False)

            if expires_at:
                log_embed.add_field(name="Expires", value=f"<t:{int(expires_at.timestamp())}:R>", inline=False)
            else:
                log_embed.add_field(name="Expires", value="Never", inline=False)

            log_embed.set_thumbnail(url=user.display_avatar.url)
            log_embed.timestamp = interaction.created_at

            await interaction.followup.send(embed=log_embed, ephemeral=True)
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

        strikes = mysql.get_strikes(user.id, interaction.guild.id)

        if not strikes:
            await interaction.followup.send(embed=Embed(
                title=f"Strikes for {user.display_name}",
                description="No strikes found for this user.",
                color=Color.red()
            ))
            return

        entries = []
        for strike in strikes:
            # Unpack the strike details
            strike_id, reason, striked_by_id, timestamp, expires_at = strike

            # Get the name of the user who issued the strike
            strike_by = interaction.guild.get_member(striked_by_id)
            strike_by_name = strike_by.display_name if strike_by else "Unknown"

            # Format the issue time
            issued_time = timestamp.strftime("%Y-%m-%d %H:%M:%S")

            # Format the expiry time using Discord's dynamic timestamp
            if expires_at:
                expiry_str = f"<t:{int(expires_at.timestamp())}:R>"  # Relative time format
            else:
                expiry_str = "Never"

            # Create the entry
            entry = {
                "title": f"Strike ID: {strike_id} | By: {strike_by_name}",
                "value": f"Reason: {reason}\nIssued: {issued_time}\nExpires: {expiry_str}"
            }
            entries.append(entry)

        # Build the content string
        content = f"Strikes for {user.display_name}:\n\n"
        for entry in entries:
            content += f"{entry['title']}\n{entry['value']}\n\n"

        # Send the response
        if len(content) > 6000:
            file = File(io.BytesIO(content.encode()), filename=f"{user.name}_strikes.txt")
            await interaction.followup.send(content="Strike list is too long, sent as a file:", file=file)
        else:
            embed = Embed(title=f"Strikes for {user.display_name}", color=Color.red())
            for entry in entries:
                embed.add_field(name=entry["title"], value=entry["value"], inline=False)
            await interaction.followup.send(embed=embed)
    
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
        _, rows_affected = mysql.execute_query(
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

    #Clear strikes
    @strike_group.command(
        name="clear",
        description="Clear all strikes of a specific user."
    )
    async def clear_strikes(self, interaction: Interaction, user: Member):
        """Clear all strikes for a specified user."""
        await interaction.response.defer(ephemeral=True)

        # Delete only active strikes from the database, we keep inactive ones for analytics
        _, rows_affected = mysql.execute_query(
            """
            DELETE FROM strikes
            WHERE user_id = %s
            AND guild_id = %s
            AND (expires_at IS NULL OR expires_at > NOW())
            """,
            (user.id, interaction.guild.id)
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
