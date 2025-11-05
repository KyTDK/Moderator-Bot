from __future__ import annotations

from typing import Optional

import discord
from discord import Interaction, Member, app_commands
from discord.ext import commands

from modules.core.moderator_bot import ModeratorBot
from modules.i18n.strings import locale_string
from modules.utils.actions import action_choices

from .actions import (
    add_action_command,
    remove_action_command,
    view_actions_command,
)
from .autocomplete import autocomplete_strike_action
from .history import clear_strikes_command, get_strikes_command
from .intimidate import intimidate_command
from .issue import issue_strike_command


class StrikesCog(commands.Cog):
    """Strike management commands."""

    strike_group = app_commands.Group(
        name="strikes",
        description=locale_string("cogs.strikes.meta.group_description"),
        default_permissions=discord.Permissions(moderate_members=True),
        guild_only=True,
    )

    def __init__(self, bot: ModeratorBot):
        self.bot = bot

    @app_commands.command(
        name="strike",
        description=locale_string("cogs.strikes.meta.strike.description"),
    )
    @app_commands.describe(
        user=locale_string("cogs.strikes.meta.strike.params.user"),
        reason=locale_string("cogs.strikes.meta.strike.params.reason"),
        expiry=locale_string("cogs.strikes.meta.strike.params.expiry"),
        skip_punishments=locale_string("cogs.strikes.meta.strike.params.skip_punishments"),
    )
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def strike(
        self,
        interaction: Interaction,
        user: Member,
        reason: str,
        expiry: Optional[str] = None,
        skip_punishments: bool = False,
    ):
        """Strike a specific user."""
        await issue_strike_command(
            self,
            interaction=interaction,
            user=user,
            reason=reason,
            expiry=expiry,
            skip_punishments=skip_punishments,
        )

    @strike_group.command(
        name="get",
        description=locale_string("cogs.strikes.meta.get.description"),
    )
    @app_commands.guild_only()
    async def get_strikes(self, interaction: Interaction, user: Member):
        """Retrieve strikes for a specified user."""
        await get_strikes_command(self, interaction=interaction, user=user)

    @strike_group.command(
        name="clear",
        description=locale_string("cogs.strikes.meta.clear.description"),
    )
    @app_commands.describe(
        user=locale_string("cogs.strikes.meta.clear.params.user"),
    )
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def clear_strikes(self, interaction: Interaction, user: Member):
        """Clear all strikes for a specified user."""
        await clear_strikes_command(self, interaction=interaction, user=user)

    @strike_group.command(
        name="add_action",
        description=locale_string("cogs.strikes.meta.add_action.description"),
    )
    @app_commands.describe(
        number_of_strikes=locale_string("cogs.strikes.meta.add_action.params.number_of_strikes"),
        action=locale_string("cogs.strikes.meta.add_action.params.action"),
        duration=locale_string("cogs.strikes.meta.add_action.params.duration"),
        channel=locale_string(
            "cogs.strikes.meta.add_action.params.channel",
            default="Channel to broadcast messages to.",
        ),
    )
    @app_commands.choices(action=action_choices(exclude=("delete", "strike")))
    async def add_strike_action(
        self,
        interaction: Interaction,
        number_of_strikes: int,
        action: str,
        duration: Optional[str] = None,
        role: Optional[discord.Role] = None,
        channel: Optional[discord.TextChannel] = None,
        reason: Optional[str] = None,
    ):
        await add_action_command(
            self,
            interaction=interaction,
            number_of_strikes=number_of_strikes,
            action=action,
            duration=duration,
            role=role,
            channel=channel,
            reason=reason,
        )

    @strike_group.command(
        name="remove_action",
        description=locale_string("cogs.strikes.meta.remove_action.description"),
    )
    @app_commands.describe(
        number_of_strikes=locale_string("cogs.strikes.meta.remove_action.params.number_of_strikes"),
        action=locale_string("cogs.strikes.meta.remove_action.params.action"),
    )
    @app_commands.autocomplete(action=autocomplete_strike_action)
    async def remove_strike_action(
        self,
        interaction: Interaction,
        number_of_strikes: int,
        action: str,
    ):
        await remove_action_command(
            self,
            interaction=interaction,
            number_of_strikes=number_of_strikes,
            action=action,
        )

    @strike_group.command(
        name="view_actions",
        description=locale_string("cogs.strikes.meta.view_actions.description"),
    )
    async def view_strike_actions(self, interaction: Interaction):
        """View all configured strike actions."""
        await view_actions_command(self, interaction=interaction)

    @app_commands.command(
        name="intimidate",
        description=locale_string("cogs.strikes.meta.intimidate.description"),
    )
    @app_commands.describe(
        user=locale_string("cogs.strikes.meta.intimidate.params.user"),
        channel=locale_string("cogs.strikes.meta.intimidate.params.channel"),
    )
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def intimidate(
        self,
        interaction: Interaction,
        user: Optional[Member] = None,
        channel: bool = False,
    ):
        """Intimidate the user or channel."""
        await intimidate_command(
            self,
            interaction=interaction,
            user=user,
            channel=channel,
        )


__all__ = ["StrikesCog"]
