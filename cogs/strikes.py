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
        description=app_commands.locale_str(
            "Strike management commands.",
            key="cogs.strikes.meta.group_description",
        ),
        default_permissions=discord.Permissions(moderate_members=True),
        guild_only=True
    )

    #strike
    @app_commands.command(
        name="strike",
        description=app_commands.locale_str(
            "Strike a specific user.",
            key="cogs.strikes.meta.strike.description",
        ),
    )
    @app_commands.describe(
        user=app_commands.locale_str(
            "The member to strike.",
            key="cogs.strikes.meta.strike.params.user",
        ),
        reason=app_commands.locale_str(
            "The reason for the strike.",
            key="cogs.strikes.meta.strike.params.reason",
        ),
        expiry=app_commands.locale_str(
            "Optional expiry duration (e.g., 30d, 2w).",
            key="cogs.strikes.meta.strike.params.expiry",
        ),
    )
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
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
            strike_texts = self.bot.translate("cogs.strikes.strike")
            await interaction.followup.send(
                strike_texts["error"],
                ephemeral=True,
            )

    @strike_group.command(
        name="get",
        description=app_commands.locale_str(
            "Get strikes of a specific user.",
            key="cogs.strikes.meta.get.description",
        ),
    )
    @app_commands.guild_only()
    async def get_strikes(self, interaction: Interaction, user: Member):
        """Retrieve strikes for a specified user."""
        await interaction.response.defer(ephemeral=True)

        strike_texts = self.bot.translate("cogs.strikes.get")
        strikes = await mysql.get_strikes(user.id, interaction.guild.id)

        if not strikes:
            await interaction.followup.send(
                embed=Embed(
                    title=strike_texts["title"].format(name=user.display_name),
                    description=strike_texts["empty"],
                    color=Color.red(),
                )
            )
            return

        entries: list[dict[str, str]] = []
        for strike_entry in strikes:
            strike_id, reason, striked_by_id, timestamp, expires_at = strike_entry
            timestamp = timestamp.replace(tzinfo=timezone.utc)
            expires_at = expires_at.replace(tzinfo=timezone.utc) if expires_at else None

            strike_by = await safe_get_user(self.bot, striked_by_id)
            strike_by_name = strike_by.display_name if strike_by else "Unknown"
            expiry_str = f"<t:{int(expires_at.timestamp())}:R>" if expires_at else "Never"

            entries.append(
                {
                    "title": f"Strike ID: {strike_id} | By: {strike_by_name}",
                    "value": f"Reason: {reason}\nIssued: <t:{int(timestamp.timestamp())}:R>\nExpires: {expiry_str}",
                }
            )

        content = strike_texts["title"].format(name=user.display_name) + "\n\n"
        for entry in entries:
            content += f"{entry['title']}\n{entry['value']}\n\n"

        if len(entries) > 25 or len(content) > 6000:
            file = File(io.BytesIO(content.encode()), filename=f"{user.name}_strikes.txt")
            await interaction.followup.send(
                content=strike_texts["file_notice"],
                file=file,
                ephemeral=True,
            )
            return

        embed = Embed(
            title=strike_texts["title"].format(name=user.display_name),
            color=Color.red(),
        )
        for entry in entries:
            embed.add_field(
                name=entry["title"],
                value=entry["value"],
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)


    # Clear strikes
    @strike_group.command(
        name="clear",
        description=app_commands.locale_str(
            "Clear all strikes of a specific user.",
            key="cogs.strikes.meta.clear.description",
        ),
    )
    @app_commands.describe(
        user=app_commands.locale_str(
            "The user whose strikes will be cleared.",
            key="cogs.strikes.meta.clear.params.user",
        ),
    )
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def clear_strikes(self, interaction: Interaction, user: Member):
        """Clear all strikes for a specified user."""
        await interaction.response.defer(ephemeral=True)

        clear_texts = self.bot.translate("cogs.strikes.clear")
        _, rows_affected = await mysql.execute_query(
            """
            DELETE FROM strikes
            WHERE user_id = %s
            AND guild_id = %s
            AND (expires_at IS NULL OR expires_at > UTC_TIMESTAMP())
            """,
            (user.id, interaction.guild.id),
        )

        if rows_affected == 0:
            await interaction.followup.send(
                clear_texts["none"].format(mention=user.mention),
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            clear_texts["success"].format(count=rows_affected, mention=user.mention),
            ephemeral=True,
        )


    @strike_group.command(
        name="add_action",
        description=app_commands.locale_str(
            "Add an additional action for a strike level.",
            key="cogs.strikes.meta.add_action.description",
        ),
    )
    @app_commands.describe(
        number_of_strikes=app_commands.locale_str(
            "Number of strikes required to trigger the action.",
            key="cogs.strikes.meta.add_action.params.number_of_strikes",
        ),
        action=app_commands.locale_str(
            "Action to add.",
            key="cogs.strikes.meta.add_action.params.action",
        ),
        duration=app_commands.locale_str(
            "Duration (only for timeout, e.g., 1h, 30m). Leave empty otherwise.",
            key="cogs.strikes.meta.add_action.params.duration",
        ),
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
            param=reason,
            translator=self.bot.translate,
        )
        if action_str is None:
            return
        texts = self.bot.translate("cogs.strikes.actions")
        actions_list = strike_actions.get(key, [])
        if action_str in actions_list:
            await interaction.followup.send(
                texts["exists"].format(action=action_str, key=key),
                ephemeral=True,
            )
            return
        actions_list.append(action_str)
        strike_actions[key] = actions_list
        await mysql.update_settings(interaction.guild.id, "strike-actions", strike_actions)
        await interaction.followup.send(
            texts["added"].format(action=action_str, key=key),
            ephemeral=True,
        )



    @strike_group.command(
        name="remove_action",
        description=app_commands.locale_str(
            "Remove an action from a strike level.",
            key="cogs.strikes.meta.remove_action.description",
        ),
    )
    @app_commands.describe(
        number_of_strikes=app_commands.locale_str(
            "Number of strikes associated with the action.",
            key="cogs.strikes.meta.remove_action.params.number_of_strikes",
        ),
        action=app_commands.locale_str(
            "Exact action string to remove.",
            key="cogs.strikes.meta.remove_action.params.action",
        ),
    )
    @app_commands.autocomplete(action=autocomplete_strike_action)
    async def remove_strike_action(self, interaction: Interaction, number_of_strikes: int, action: str):
        await interaction.response.defer(ephemeral=True)
        strike_actions = await mysql.get_settings(interaction.guild.id, "strike-actions") or {}
        key = str(number_of_strikes)
        actions_list = strike_actions.get(key)
        texts = self.bot.translate("cogs.strikes.actions")
        if not actions_list or action not in actions_list:
            await interaction.followup.send(
                texts["missing"].format(action=action, key=key),
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
            texts["removed"].format(action=action, key=key),
            ephemeral=True,
        )



    @strike_group.command(
        name="view_actions",
        description=app_commands.locale_str(
            "View all configured strike actions.",
            key="cogs.strikes.meta.view_actions.description",
        ),
    )
    async def view_strike_actions(self, interaction: Interaction):
        actions_texts = self.bot.translate("cogs.strikes.view_actions")
        strike_actions = await mysql.get_settings(interaction.guild.id, "strike-actions") or {}
        if not strike_actions:
            await interaction.response.send_message(actions_texts["none"], ephemeral=True)
            return
        lines = []
        for k in sorted(strike_actions.keys(), key=int):
            actions = ", ".join(strike_actions[k])
            lines.append(actions_texts["item"].format(key=k, actions=actions))
        await interaction.response.send_message(
            actions_texts["heading"] + "\n" + "\n".join(lines),
            ephemeral=True,
        )



    # Warn channel or optionally user
    @app_commands.command(
        name="intimidate",
        description=app_commands.locale_str(
            "Intimidate the channel, or a specific user.",
            key="cogs.strikes.meta.intimidate.description",
        ),
    )
    @app_commands.describe(
        user=app_commands.locale_str(
            "User to intimidate. Leave empty to address the whole channel.",
            key="cogs.strikes.meta.intimidate.params.user",
        ),
        channel=app_commands.locale_str(
            "Send the warning in the channel instead of a direct message.",
            key="cogs.strikes.meta.intimidate.params.channel",
        )
    )
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def intimidate(self, interaction: Interaction, user: Member = None, channel: bool = False):
        """Intimidate the user."""
        intimidate_texts = self.bot.translate("cogs.strikes.intimidate")
        if user:
            embed = Embed(
                title=intimidate_texts["user_title"].format(name=user.display_name),
                description=intimidate_texts["user_body"].format(mention=user.mention),
                color=Color.red(),
            )
            if channel:
                await interaction.channel.send(embed=embed)
            else:
                await user.send(embed=embed)
        else:
            embed = Embed(
                title=intimidate_texts["guild_title"],
                description=intimidate_texts["guild_body"],
                color=Color.red(),
            )
            await interaction.channel.send(embed=embed)
        await interaction.response.send_message(intimidate_texts["confirm"], ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(StrikesCog(bot))
