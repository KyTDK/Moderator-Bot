from discord import Interaction, Member, Embed, Color
from discord.ext import commands
from modules.utils.user_utils import message_user
from modules.utils.mysql import execute_query
from dotenv import load_dotenv
from datetime import datetime, timedelta
from discord.utils import utcnow
from modules.utils import logging
import os

load_dotenv()
GUILD_ID = int(os.getenv('GUILD_ID'))
STRIKES_CHANNEL_ID = int(os.getenv('STRIKES_CHANNEL_ID'))

async def strike(user: Member, bot: commands.Bot, reason: str = "No reason provided", interaction: Interaction = None) -> bool:
    """strike a specific user with escalating consequences."""
    # Determine the issuer and the target guild
    if interaction:
        await interaction.response.defer(ephemeral=True)
        strike_by = interaction.user
        guild = interaction.guild
    else:
        strike_by = bot.user
        guild = bot.get_guild(GUILD_ID)

    if guild is None:
        print(f"Guild with ID {GUILD_ID} not found.")
        return False

    # Record the strike in the database
    execute_query(
        "INSERT INTO strikes (user_id, reason, striked_by_id, timestamp) VALUES (%s, %s, %s, %s)",
        (user.id, reason, strike_by.id, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    )

    # Fetch the updated strike count for the user
    result = execute_query(
        "SELECT COUNT(*) FROM strikes WHERE user_id = %s",
        (user.id,), fetch_one=True
    )
    strike_count = result[0][0] if result else 0
    
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
        await message_user(user, "", bot, guild, embed=embed)
    except Exception as e:
        error_message = f"Unable to send strike message to {user.mention}: {e}"
        print(error_message)
        if interaction:
            await interaction.followup.send(error_message, ephemeral=True)
        return False


    # Apply disciplinary action and capture a description
    try:
        if action == "timeout":
            until = utcnow() + duration
            await user.timeout(until, reason=reason)
        elif action == "ban":
            await guild.ban(user, reason=reason)
    except Exception as e:
        print(f"Failed to apply disciplinary action for user {user}: {e}")
        return True

    # Log strikes channel
    embed.title = f"{user.display_name} received a strike"
    await logging.log_to_channel(embed, STRIKES_CHANNEL_ID, bot)

    return True
