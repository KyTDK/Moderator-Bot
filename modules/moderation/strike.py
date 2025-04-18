from discord import Interaction, Member, Embed, Color
from discord.ext import commands
from modules.utils.user_utils import message_user
from modules.utils.mysql import execute_query
from datetime import datetime, timedelta
from discord.utils import utcnow
from modules.utils import logging
from modules.utils import mysql
from modules.utils.time import parse_duration

def get_ban_threshold(strike_settings):
    """
    Given a settings dict mapping strike numbers to an action and duration,
    returns the strike count when a "ban" is applied (e.g., 'Ban') or None if no ban is set.
    """
    # Get the available strike thresholds as integers
    available_strikes = sorted(strike_settings.keys(), key=int)
    
    # Iterate over each strike threshold in ascending order
    for strike in available_strikes:
        action, duration_str = strike_settings[strike]
        if action.lower() == "ban":
            return int(strike)
    return None

async def strike(user: Member, bot: commands.Bot, reason: str = "No reason provided", interaction: Interaction = None) -> bool:
    """Strike a specific user with escalating consequences based on settings."""
    if interaction:
        await interaction.response.defer(ephemeral=True)
        strike_by = interaction.user
    else:
        strike_by = bot.user

    # Record the strike in the database.
    guild_id = user.guild.id
    execute_query(
        "INSERT INTO strikes (guild_id, user_id, reason, striked_by_id, timestamp) VALUES (%s, %s, %s, %s, %s)",
        (guild_id, user.id, reason, strike_by.id, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    )

    # Fetch the updated strike count for the user.
    strike_count = mysql.get_strike_count(user.id, guild_id)

    # Limit strike count
    if interaction and strike_count>100:
            interaction.followup.send("You cannot give the same player more than 100 strikes. Use `strikes clear <user>` to reset their strikes.")
            return
    
    now = utcnow()

    # Retrieve the strike actions setting; if not found, use the hardcoded default.
    strike_settings = mysql.get_settings(guild_id, "strike-actions")
    
    # Get the approriate action based on the strike count.
    # strike_settings = {"1": ["timeout", "1d"], "2": ["Timeout", "7d"], "3": ["Ban", "-1"], "1": ["timeout", "1d"]}}
    available_strikes = sorted(strike_settings.keys(), key=int)

    action, duration_str = strike_settings.get(str(strike_count), (None, None))

    strikes_for_ban = get_ban_threshold(strike_settings)
    strikes_till_ban =  strikes_for_ban - strike_count if strikes_for_ban is not None else None

    duration = parse_duration(duration_str)

    if action is not None:
        action = action.lower()

    # Build the action description.
    if action == "timeout":
        if duration is None:
            # Fallback duration if parsing fails.
            duration = timedelta(days=1)
        until = now + duration
        action_description = f"\n**Action Taken:** Timed out until __{until.strftime('%A, %B %d at %I:%M %p %Z')}__"
    elif action == "ban":
        action_description = "\n**Action Taken:** Banned from the server"
    elif action == "kick":
        action_description = "\n**Action Taken:** Kicked from the server"
    else:
        action_description = "\n**Action Taken:** No action applied"


    # Add strike information.
    if strike_count < len(available_strikes):
        strike_info = f"\n**Strike Count:** {strike_count} strike(s)."
        if strikes_till_ban:
            strike_info += f" {strikes_till_ban} more strike(s) before a permanent ban."
    else:
        strike_info = f"\n**Strike Count:** {strike_count} strike(s)."

    # Construct the embed message for the user.
    embed = Embed(
        title="⚠️ You have received a strike",
        description=f"**Reason:** {reason}{action_description}{strike_info}",
        color=Color.red(),
        timestamp=now
    )
    embed.set_footer(text=f"Strike by {strike_by.display_name}", icon_url=strike_by.display_avatar.url)

    # Attempt to send the strike message.
    try:
        await message_user(user, "", embed=embed)
    except Exception as e:
        if interaction:
            await interaction.channel.send(user.mention, embed=embed)
        return True

    # Apply the disciplinary action.
    try:
        if action == "timeout":
            until = utcnow() + duration
            await user.timeout(until, reason=reason)
        elif action == "ban":
            await user.ban(reason=reason)
        elif action == "kick":
            await user.kick(reason=reason)
    except Exception as e:
        print(f"Failed to apply disciplinary action for user {user}: {e}")

    # Log the strike in a designated strikes channel.
    embed.title = f"{user.display_name} received a strike"
    settings = mysql.get_settings(user.guild.id)
    STRIKES_CHANNEL_ID = settings.get("strike-channel") if settings else None
    if STRIKES_CHANNEL_ID:
        await logging.log_to_channel(embed, STRIKES_CHANNEL_ID, bot)

    return True