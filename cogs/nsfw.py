import discord
from discord.ext import commands
from discord import app_commands, Interaction

from modules.i18n.strings import locale_namespace
from modules.utils import mysql
from modules.utils.action_manager import ActionListManager
from modules.utils.discord_utils import require_accelerated
from modules.utils.list_manager import ListManager
from modules.utils.strike import validate_action
from modules.utils.actions import action_choices, VALID_ACTION_VALUES
from modules.core.moderator_bot import ModeratorBot

NSFW_ACTION_SETTING = "nsfw-detection-action"
manager = ActionListManager(NSFW_ACTION_SETTING)
NSFW_CATEGORY_SETTING = "nsfw-detection-categories"
category_manager = ListManager(NSFW_CATEGORY_SETTING)

NSFW_LOCALE = locale_namespace("cogs", "nsfw")
NSFW_META = NSFW_LOCALE.child("meta")
NSFW_CATEGORY_LABELS = NSFW_META.child("categories")

class NSFWCog(commands.Cog):
    def __init__(self, bot: ModeratorBot):
        self.bot = bot

    nsfw_group = app_commands.Group(
        name="nsfw",
        description=NSFW_META.string("group_description"),
        guild_only=True,
        default_permissions=discord.Permissions(manage_messages=True),
    )

    @nsfw_group.command(
        name="add_action",
        description=NSFW_META.string("add_action", "description"),
    )
    @app_commands.describe(
        action=NSFW_META.child("add_action", "params").string("action"),
        duration=NSFW_META.child("add_action", "params").string("duration"),
    )
    @app_commands.choices(action=action_choices())
    async def add_nsfw_action(
        self,
        interaction: Interaction,
        action: str,
        duration: str = None,
        role: discord.Role = None,
        reason: str = None,
    ):
        await interaction.response.defer(ephemeral=True)
        
        gid = interaction.guild.id
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

        message = await manager.add_action(gid, action_str, translator=self.bot.translate)
        await interaction.followup.send(message, ephemeral=True)

    @nsfw_group.command(
        name="remove_action",
        description=NSFW_META.string("remove_action", "description"),
    )
    @app_commands.describe(
        action=NSFW_META.child("remove_action", "params").string("action")
    )
    @app_commands.autocomplete(action=manager.autocomplete)
    async def remove_nsfw_action(self, interaction: Interaction, action: str):
        gid = interaction.guild.id

        message = await manager.remove_action(gid, action, translator=self.bot.translate)
        await interaction.response.send_message(message, ephemeral=True)

    @nsfw_group.command(
        name="view_actions",
        description=NSFW_META.string("view_actions", "description"),
    )
    async def view_nsfw_actions(self, interaction: Interaction):
        gid = interaction.guild.id

        actions = await manager.view_actions(gid)
        texts = self.bot.translate("cogs.nsfw.actions",
                                    guild_id=gid)
        if not actions:
            await interaction.response.send_message(texts["none"], ephemeral=True)
            return

        formatted = "\n".join(f"{i+1}. `{a}`" for i, a in enumerate(actions))
        await interaction.response.send_message(
            texts["heading"].format(actions=formatted),
            ephemeral=True
        )

    @nsfw_group.command(
        name="add_category",
        description=NSFW_META.string("add_category", "description"),
    )
    @app_commands.choices(
        category=[
            app_commands.Choice(
                name=NSFW_CATEGORY_LABELS.string("violence_graphic"),
                value="violence_graphic",
            ),
            app_commands.Choice(
                name=NSFW_CATEGORY_LABELS.string("violence"),
                value="violence",
            ),
            app_commands.Choice(
                name=NSFW_CATEGORY_LABELS.string("sexual"),
                value="sexual",
            ),
            app_commands.Choice(
                name=NSFW_CATEGORY_LABELS.string("self_harm_instructions"),
                value="self_harm_instructions",
            ),
            app_commands.Choice(
                name=NSFW_CATEGORY_LABELS.string("self_harm_intent"),
                value="self_harm_intent",
            ),
            app_commands.Choice(
                name=NSFW_CATEGORY_LABELS.string("self_harm"),
                value="self_harm",
            ),
        ]
    )
    async def add_category(self, interaction: Interaction, category: str):
        # Accelerated only
        if not await require_accelerated(interaction):
            return
        # Continue with adding category
        gid = interaction.guild.id
        message = await category_manager.add(gid, category, translator=self.bot.translate)
        await interaction.response.send_message(message, ephemeral=True)

    @nsfw_group.command(
        name="remove_category",
        description=NSFW_META.string("remove_category", "description"),
    )
    @app_commands.autocomplete(category=category_manager.autocomplete)
    async def remove_category(self, interaction: Interaction, category: str):
        # Accelerated only
        if not await require_accelerated(interaction):
            return
        # Continue with adding category
        gid = interaction.guild.id
        message = await category_manager.remove(gid, category, translator=self.bot.translate)
        await interaction.response.send_message(message, ephemeral=True)

    @nsfw_group.command(
        name="view_categories",
        description=NSFW_META.string("view_categories", "description"),
    )
    async def view_categories(self, interaction: Interaction):
        gid = interaction.guild.id
        categories = await category_manager.view(gid)
        texts = self.bot.translate("cogs.nsfw.categories",
                                    guild_id=gid)
        if not categories:
            await interaction.response.send_message(texts["none"], ephemeral=True)
            return
        formatted = "\n".join(f"{i+1}. `{c}`" for i, c in enumerate(categories))
        await interaction.response.send_message(
            texts["heading"].format(categories=formatted),
            ephemeral=True
        )

    @nsfw_group.command(
        name="set_threshold",
        description=NSFW_META.string("set_threshold", "description"),
    )
    @app_commands.describe(
        threshold=NSFW_META.child("set_threshold", "params").string("threshold")
    )
    async def set_threshold(self, interaction: Interaction, threshold: float):
        guild_id = interaction.guild.id
        threshold_texts = self.bot.translate("cogs.nsfw.threshold",
                                            guild_id=guild_id)
        if not (0.0 <= threshold <= 1.0):
            await interaction.response.send_message(
                threshold_texts["invalid"],
                ephemeral=True,
            )
            return

        gid = interaction.guild.id
        await mysql.update_settings(gid, "threshold", threshold)
        await interaction.response.send_message(
            threshold_texts["set"].format(value=threshold),
            ephemeral=True,
        )

    @nsfw_group.command(
        name="view_threshold",
        description=NSFW_META.string("view_threshold", "description"),
    )
    async def view_threshold(self, interaction: Interaction):
        gid = interaction.guild.id
        threshold = await mysql.get_settings(gid, "threshold")
        texts = self.bot.translate("cogs.nsfw.threshold",
                                   guild_id=gid)
        if threshold is None:
            await interaction.response.send_message(texts["unset"], ephemeral=True)
            return

        await interaction.response.send_message(
            texts["current"].format(value=threshold),
            ephemeral=True,
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(NSFWCog(bot))