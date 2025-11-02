import discord
from discord.ext import commands
from discord import app_commands, Interaction

from modules.i18n.strings import locale_namespace
from modules.utils import mysql
from modules.utils.action_manager import ActionListManager
from modules.utils.discord_utils import require_accelerated
from modules.utils.list_manager import ListManager
from modules.utils.actions import action_choices, VALID_ACTION_VALUES
from modules.core.moderator_bot import ModeratorBot
from modules.utils.action_command_helpers import (
    process_add_action,
    process_remove_action,
    process_view_actions,
)
from modules.nsfw_scanner.settings_keys import (
    NSFW_ACTION_SETTING,
    NSFW_IMAGE_CATEGORY_SETTING,
    NSFW_TEXT_ACTION_SETTING,
    NSFW_TEXT_CATEGORY_SETTING,
    NSFW_TEXT_ENABLED_SETTING,
    NSFW_TEXT_STRIKES_ONLY_SETTING,
    NSFW_TEXT_THRESHOLD_SETTING,
)

manager = ActionListManager(NSFW_ACTION_SETTING)
NSFW_CATEGORY_SETTING = NSFW_IMAGE_CATEGORY_SETTING
category_manager = ListManager(NSFW_CATEGORY_SETTING)
TEXT_CATEGORY_SETTING = NSFW_TEXT_CATEGORY_SETTING
text_category_manager = ListManager(TEXT_CATEGORY_SETTING)
TEXT_THRESHOLD_SETTING = NSFW_TEXT_THRESHOLD_SETTING
text_action_manager = ActionListManager(NSFW_TEXT_ACTION_SETTING)


def _parse_bool_setting(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)

NSFW_LOCALE = locale_namespace("cogs", "nsfw")
NSFW_META = NSFW_LOCALE.child("meta")
NSFW_CATEGORY_LABELS = NSFW_META.child("categories")

IMAGE_CATEGORY_CHOICES = [
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

TEXT_CATEGORY_CHOICES = IMAGE_CATEGORY_CHOICES + [
    app_commands.Choice(
        name=NSFW_CATEGORY_LABELS.string("sexual_minors"),
        value="sexual_minors",
    ),
    app_commands.Choice(
        name=NSFW_CATEGORY_LABELS.string("harassment"),
        value="harassment",
    ),
    app_commands.Choice(
        name=NSFW_CATEGORY_LABELS.string("harassment_threatening"),
        value="harassment_threatening",
    ),
    app_commands.Choice(
        name=NSFW_CATEGORY_LABELS.string("hate"),
        value="hate",
    ),
    app_commands.Choice(
        name=NSFW_CATEGORY_LABELS.string("hate_threatening"),
        value="hate_threatening",
    ),
    app_commands.Choice(
        name=NSFW_CATEGORY_LABELS.string("illicit"),
        value="illicit",
    ),
    app_commands.Choice(
        name=NSFW_CATEGORY_LABELS.string("illicit_violent"),
        value="illicit_violent",
    ),
]

class NSFWCog(commands.Cog):
    def __init__(self, bot: ModeratorBot):
        self.bot = bot

    async def _add_category_setting(self, interaction: Interaction, manager: ListManager, category: str) -> None:
        if not await require_accelerated(interaction):
            return
        gid = interaction.guild.id
        message = await manager.add(gid, category, translator=self.bot.translate)
        await interaction.response.send_message(message, ephemeral=True)

    async def _remove_category_setting(self, interaction: Interaction, manager: ListManager, category: str) -> None:
        if not await require_accelerated(interaction):
            return
        gid = interaction.guild.id
        message = await manager.remove(gid, category, translator=self.bot.translate)
        await interaction.response.send_message(message, ephemeral=True)

    async def _view_category_setting(self, interaction: Interaction, manager: ListManager, translation_key: str) -> None:
        gid = interaction.guild.id
        categories = await manager.view(gid)
        texts = self.bot.translate(translation_key, guild_id=gid)
        if not categories:
            await interaction.response.send_message(texts["none"], ephemeral=True)
            return
        formatted = "\n".join(f"{i+1}. `{c}`" for i, c in enumerate(categories))
        await interaction.response.send_message(
            texts["heading"].format(categories=formatted),
            ephemeral=True,
        )

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
        channel=NSFW_META.child("add_action", "params").string(
            "channel",
            default="Channel to broadcast messages to.",
        ),
    )
    @app_commands.choices(action=action_choices())
    async def add_nsfw_action(
        self,
        interaction: Interaction,
        action: str,
        duration: str = None,
        role: discord.Role = None,
        channel: discord.TextChannel = None,
        reason: str = None,
    ):
        gid = interaction.guild.id
        await process_add_action(
            interaction,
            manager=manager,
            translator=self.bot.translate,
            validate_kwargs={
                "interaction": interaction,
                "action": action,
                "duration": duration,
                "role": role,
                "channel": channel,
                "valid_actions": VALID_ACTION_VALUES,
                "param": reason,
                "translator": self.bot.translate,
            },
        )

    @nsfw_group.command(
        name="add_text_action",
        description=NSFW_META.string("add_text_action", "description"),
    )
    @app_commands.describe(
        action=NSFW_META.child("add_text_action", "params").string("action"),
        duration=NSFW_META.child("add_text_action", "params").string("duration"),
        channel=NSFW_META.child("add_text_action", "params").string(
            "channel",
            default="Channel to broadcast messages to.",
        ),
    )
    @app_commands.choices(action=action_choices())
    async def add_text_action(
        self,
        interaction: Interaction,
        action: str,
        duration: str = None,
        role: discord.Role = None,
        channel: discord.TextChannel = None,
        reason: str = None,
    ):
        if not await require_accelerated(interaction):
            return
        await process_add_action(
            interaction,
            manager=text_action_manager,
            translator=self.bot.translate,
            validate_kwargs={
                "interaction": interaction,
                "action": action,
                "duration": duration,
                "role": role,
                "channel": channel,
                "valid_actions": VALID_ACTION_VALUES,
                "param": reason,
                "translator": self.bot.translate,
            },
        )

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

        await process_remove_action(
            interaction,
            manager=manager,
            translator=self.bot.translate,
            action=action,
        )

    @nsfw_group.command(
        name="remove_text_action",
        description=NSFW_META.string("remove_text_action", "description"),
    )
    @app_commands.describe(
        action=NSFW_META.child("remove_text_action", "params").string("action")
    )
    @app_commands.autocomplete(action=text_action_manager.autocomplete)
    async def remove_text_action(self, interaction: Interaction, action: str):
        if not await require_accelerated(interaction):
            return
        await process_remove_action(
            interaction,
            manager=text_action_manager,
            translator=self.bot.translate,
            action=action,
        )

    @nsfw_group.command(
        name="view_actions",
        description=NSFW_META.string("view_actions", "description"),
    )
    async def view_nsfw_actions(self, interaction: Interaction):
        gid = interaction.guild.id

        texts = self.bot.translate("cogs.nsfw.actions", guild_id=gid)

        await process_view_actions(
            interaction,
            manager=manager,
            when_empty=texts["none"],
            format_message=lambda actions: texts["heading"].format(
                actions="\n".join(f"{i + 1}. `{a}`" for i, a in enumerate(actions))
            ),
        )

    @nsfw_group.command(
        name="view_text_actions",
        description=NSFW_META.string("view_text_actions", "description"),
    )
    async def view_text_actions(self, interaction: Interaction):
        if not await require_accelerated(interaction):
            return
        gid = interaction.guild.id
        texts = self.bot.translate("cogs.nsfw.text_actions", guild_id=gid)
        await process_view_actions(
            interaction,
            manager=text_action_manager,
            when_empty=texts["none"],
            format_message=lambda actions: texts["heading"].format(
                actions="\n".join(f"{i + 1}. `{a}`" for i, a in enumerate(actions))
            ),
        )

    @nsfw_group.command(
        name="add_category",
        description=NSFW_META.string("add_category", "description"),
    )
    @app_commands.choices(category=IMAGE_CATEGORY_CHOICES)
    async def add_category(self, interaction: Interaction, category: str):
        await self._add_category_setting(interaction, category_manager, category)

    @nsfw_group.command(
        name="remove_category",
        description=NSFW_META.string("remove_category", "description"),
    )
    @app_commands.autocomplete(category=category_manager.autocomplete)
    async def remove_category(self, interaction: Interaction, category: str):
        await self._remove_category_setting(interaction, category_manager, category)

    @nsfw_group.command(
        name="view_categories",
        description=NSFW_META.string("view_categories", "description"),
    )
    async def view_categories(self, interaction: Interaction):
        await self._view_category_setting(interaction, category_manager, "cogs.nsfw.categories")

    @nsfw_group.command(
        name="add_text_category",
        description=NSFW_META.string("add_text_category", "description"),
    )
    @app_commands.choices(category=TEXT_CATEGORY_CHOICES)
    async def add_text_category(self, interaction: Interaction, category: str):
        await self._add_category_setting(interaction, text_category_manager, category)

    @nsfw_group.command(
        name="remove_text_category",
        description=NSFW_META.string("remove_text_category", "description"),
    )
    @app_commands.autocomplete(category=text_category_manager.autocomplete)
    async def remove_text_category(self, interaction: Interaction, category: str):
        await self._remove_category_setting(interaction, text_category_manager, category)

    @nsfw_group.command(
        name="view_text_categories",
        description=NSFW_META.string("view_text_categories", "description"),
    )
    async def view_text_categories(self, interaction: Interaction):
        await self._view_category_setting(
            interaction,
            text_category_manager,
            "cogs.nsfw.text_categories",
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
        name="set_text_threshold",
        description=NSFW_META.string("set_text_threshold", "description"),
    )
    @app_commands.describe(
        threshold=NSFW_META.child("set_text_threshold", "params").string("threshold")
    )
    async def set_text_threshold(self, interaction: Interaction, threshold: float):
        if not await require_accelerated(interaction):
            return
        guild_id = interaction.guild.id
        threshold_texts = self.bot.translate("cogs.nsfw.text_threshold",
                                            guild_id=guild_id)
        if not (0.0 <= threshold <= 1.0):
            await interaction.response.send_message(
                threshold_texts["invalid"],
                ephemeral=True,
            )
            return

        await mysql.update_settings(guild_id, TEXT_THRESHOLD_SETTING, threshold)
        await interaction.response.send_message(
            threshold_texts["set"].format(value=threshold),
            ephemeral=True,
        )

    @nsfw_group.command(
        name="set_text_scanning",
        description=NSFW_META.string("set_text_scanning", "description"),
    )
    @app_commands.describe(
        enabled=NSFW_META.child("set_text_scanning", "params").string("enabled")
    )
    async def set_text_scanning(self, interaction: Interaction, enabled: bool):
        if not await require_accelerated(interaction):
            return
        guild_id = interaction.guild.id
        texts = self.bot.translate("cogs.nsfw.text_scanning", guild_id=guild_id)
        await mysql.update_settings(guild_id, NSFW_TEXT_ENABLED_SETTING, enabled)
        message = texts["enabled" if enabled else "disabled"]
        await interaction.response.send_message(message, ephemeral=True)

    @nsfw_group.command(
        name="view_text_scanning",
        description=NSFW_META.string("view_text_scanning", "description"),
    )
    async def view_text_scanning(self, interaction: Interaction):
        guild_id = interaction.guild.id
        texts = self.bot.translate("cogs.nsfw.text_scanning", guild_id=guild_id)
        enabled_value = await mysql.get_settings(guild_id, NSFW_TEXT_ENABLED_SETTING)
        enabled = _parse_bool_setting(enabled_value, default=False)
        await interaction.response.send_message(
            texts["status"].format(state=texts["enabled_label" if enabled else "disabled_label"]),
            ephemeral=True,
        )

    @nsfw_group.command(
        name="set_text_strike_filter",
        description=NSFW_META.string("set_text_strike_filter", "description"),
    )
    @app_commands.describe(
        enabled=NSFW_META.child("set_text_strike_filter", "params").string("enabled")
    )
    async def set_text_strike_filter(self, interaction: Interaction, enabled: bool):
        if not await require_accelerated(interaction):
            return
        guild_id = interaction.guild.id
        texts = self.bot.translate("cogs.nsfw.text_strike_filter", guild_id=guild_id)
        await mysql.update_settings(guild_id, NSFW_TEXT_STRIKES_ONLY_SETTING, enabled)
        await interaction.response.send_message(
            texts["enabled" if enabled else "disabled"],
            ephemeral=True,
        )

    @nsfw_group.command(
        name="view_text_strike_filter",
        description=NSFW_META.string("view_text_strike_filter", "description"),
    )
    async def view_text_strike_filter(self, interaction: Interaction):
        if not await require_accelerated(interaction):
            return
        guild_id = interaction.guild.id
        texts = self.bot.translate("cogs.nsfw.text_strike_filter", guild_id=guild_id)
        enabled_value = await mysql.get_settings(guild_id, NSFW_TEXT_STRIKES_ONLY_SETTING)
        enabled = _parse_bool_setting(enabled_value, default=False)
        await interaction.response.send_message(
            texts["status"].format(
                state=texts["enabled_label" if enabled else "disabled_label"]
            ),
            ephemeral=True,
        )

    @nsfw_group.command(
        name="view_text_threshold",
        description=NSFW_META.string("view_text_threshold", "description"),
    )
    async def view_text_threshold(self, interaction: Interaction):
        guild_id = interaction.guild.id
        texts = self.bot.translate("cogs.nsfw.text_threshold",
                                   guild_id=guild_id)
        threshold = await mysql.get_settings(guild_id, TEXT_THRESHOLD_SETTING)
        if threshold is None:
            await interaction.response.send_message(texts["unset"], ephemeral=True)
            return

        await interaction.response.send_message(
            texts["current"].format(value=threshold),
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
