from discord import Interaction, Member, Embed, Color
from discord.ext import commands
from modules.utils.user_utils import message_user
from modules.utils.mysql import execute_query
from datetime import datetime, timedelta
from discord.utils import utcnow
from modules.utils import logging
from modules.utils import mysql
import json

async def strike(user: Member, bot: commands.Bot, reason: str = "No reason provided", interaction: Interaction = None) -> bool:
    """strike a specific user with escalating consequences."""
    if interaction:
        await interaction.response.defer(ephemeral=True)
        strike_by = interaction.user
    else:
        strike_by = bot.user
    # Record the strike in the database
    guild_id = user.guild.id
    execute_query(
        "INSERT INTO strikes (guild_id, user_id, reason, striked_by_id, timestamp) VALUES (%s, %s, %s, %s, %s)",
        (guild_id, user.id, reason, strike_by.id, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    )

    # Fetch the updated strike count for the user
    strike_count = mysql.get_strike_count(user.id, guild_id)
    
    now = utcnow()

    # Determine disciplinary action based on strike count
    if strike_count == 1:
        duration = timedelta(days=1)
        action = "timeout"
        until = now + duration
        action_description = f"\n**Action Taken:** Timed out until __{until.strftime('%A, %B %d at %I:%M %p %Z')}__"
    elif strike_count == 2:
        duration = timedelta(weeks=1)
        action = "timeout"
        until = now + duration
        action_description = f"\n**Action Taken:** Timed out until __{until.strftime('%A, %B %d at %I:%M %p %Z')}__"
    elif strike_count >= 3:
        action = "ban"
        action_description = "\n**Action Taken:** Banned from the server"
    else:
        print("Invalid strike count.")
        return False

    # Add strike count and remaining strikes info
    if strike_count < 3:
        strikes_remaining = 3 - strike_count
        strike_info = f"\n**Strike Count:** {strike_count} strike(s). {strikes_remaining} more strike(s) before a permanent ban."
    else:
        strike_info = f"\n**Strike Count:** {strike_count} strike(s)."
    
    # Construct the embed message
    embed = Embed(
        title="⚠️ You have received a strike",
        description=f"**Reason:** {reason}{action_description}{strike_info}",
        color=Color.red(),
        timestamp=now
    )
    embed.set_footer(text=f"Strike by {strike_by.display_name}", icon_url=strike_by.display_avatar.url)

    # Send the strike message to the user
    try:
        await message_user(user, "", embed=embed)
    except Exception as e:
        if interaction:
            await interaction.channel.send(user.mention, embed=embed)      
        return True

    # Apply disciplinary action and capture a description
    try:
        if action == "timeout":
            until = utcnow() + duration
            await user.timeout(until, reason=reason)
        elif action == "ban":
            await user.ban(reason=reason)
    except Exception as e:
        print(f"Failed to apply disciplinary action for user {user}: {e}")

    # Log strikes channel
    embed.title = f"{user.display_name} received a strike"
    STRIKES_CHANNEL_ID = mysql.get_settings(user.guild.id).get("strike_channel")
    if STRIKES_CHANNEL_ID:
        await logging.log_to_channel(embed, STRIKES_CHANNEL_ID, bot)

    return True
