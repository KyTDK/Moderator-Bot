import discord
from discord.ext import commands
from discord import app_commands, Interaction
from collections import defaultdict

from modules.utils import mysql
from modules.utils.event_manager import EventListManager
from modules.utils.action_manager import ActionListManager
from modules.utils.actions import VALID_ACTION_VALUES, action_choices
from modules.utils.strike import validate_action
from modules.core.moderator_bot import ModeratorBot
from modules.i18n.strings import locale_string

AIMOD_ACTION_SETTING = "aimod-detection-action"
ACTION_MANAGER = ActionListManager(AIMOD_ACTION_SETTING)

ADAPTIVE_EVENTS = [
    ("Role Online", "role_online", "cogs.autonomous_moderation.meta.adaptive.events.role_online"),
    ("Role Offline", "role_offline", "cogs.autonomous_moderation.meta.adaptive.events.role_offline"),
    ("Mass Join", "mass_join", "cogs.autonomous_moderation.meta.adaptive.events.mass_join"),
    ("Mass Leave", "mass_leave", "cogs.autonomous_moderation.meta.adaptive.events.mass_leave"),
    ("Server Inactivity", "guild_inactive", "cogs.autonomous_moderation.meta.adaptive.events.guild_inactive"),
    ("Role Online %", "role_online_percent", "cogs.autonomous_moderation.meta.adaptive.events.role_online_percent"),
    ("Time Range", "time_range", "cogs.autonomous_moderation.meta.adaptive.events.time_range"),
    ("Server Spike", "server_spike", "cogs.autonomous_moderation.meta.adaptive.events.server_spike"),
]
VALID_ADAPTIVE_EVENTS = {value: key for _, value, key in ADAPTIVE_EVENTS}
ACTIONS = [
    ("Enable Interval Mode", "enable_interval", "cogs.autonomous_moderation.meta.actions.enable_interval"),
    ("Disable Interval Mode", "disable_interval", "cogs.autonomous_moderation.meta.actions.disable_interval"),
    ("Enable Report Mode", "enable_report", "cogs.autonomous_moderation.meta.actions.enable_report"),
    ("Disable Report Mode", "disable_report", "cogs.autonomous_moderation.meta.actions.disable_report"),
]
ACTION_LABEL_KEYS = {value: key for _, value, key in ACTIONS}

AIMOD_EVENT_SETTING = "aimod-adaptive-events"
EVENT_MANAGER = EventListManager(AIMOD_EVENT_SETTING)


def _translate(interaction: Interaction, key: str):
    bot = interaction.client
    return bot.translate(key)


async def can_run(interaction: Interaction) -> bool:
    texts = _translate(interaction, "cogs.autonomous_moderation.common")
    guild_id = interaction.guild.id
    accelerated = await mysql.is_accelerated(guild_id=guild_id)

    if not accelerated:
        msg = texts["subscription_required"]
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return False

    return True


class AutonomousCommandsCog(commands.Cog):
    def __init__(self, bot: ModeratorBot):
        self.bot = bot

    ai_mod_group = app_commands.Group(
        name="ai_mod",
        description=locale_string("cogs.autonomous_moderation.meta.group_description"),
        default_permissions=discord.Permissions(manage_messages=True),
        guild_only=True,
    )

    @ai_mod_group.command(
        name="rules_set",
        description=locale_string("cogs.autonomous_moderation.meta.rules_set.description"),
    )
    @app_commands.default_permissions(manage_guild=True)
    async def set_rules(self, interaction: Interaction, *, rules: str):
        if not await can_run(interaction):
            return
        guild_id = interaction.guild.id
        await mysql.update_settings(interaction.guild.id, "rules", rules)
        texts = self.bot.translate("cogs.autonomous_moderation.rules",
                                   guild_id=guild_id)
        await interaction.response.send_message(texts["updated"], ephemeral=True)

    @ai_mod_group.command(
        name="rules_show",
        description=locale_string("cogs.autonomous_moderation.meta.rules_show.description"),
    )
    async def show_rules(self, interaction: Interaction):
        if not await can_run(interaction):
            return
        guild_id = interaction.guild.id
        texts = self.bot.translate("cogs.autonomous_moderation.rules",
                                   guild_id=guild_id)
        rules = await mysql.get_settings(interaction.guild.id, "rules")
        if not rules:
            await interaction.response.send_message(texts["none"], ephemeral=True)
            return

        if len(rules) > 1900:
            rules = rules[:1900] + texts["truncated_suffix"]

        await interaction.response.send_message(
            texts["heading"].format(rules=rules),
            ephemeral=True,
        )

    @ai_mod_group.command(
        name="add_action",
        description=locale_string("cogs.autonomous_moderation.meta.add_action.description"),
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.choices(action=action_choices(include=[("Auto", "auto")]))
    async def add_action(
        self,
        interaction: Interaction,
        action: str,
        duration: str = None,
        role: discord.Role = None,
        reason: str = None,
    ):
        await interaction.response.defer(ephemeral=True)
        if not await can_run(interaction):
            return

        action_str = await validate_action(
            interaction=interaction,
            action=action,
            duration=duration,
            role=role,
            valid_actions=VALID_ACTION_VALUES + ["auto"],
            param=reason,
            translator=self.bot.translate,
        )
        if action_str is None:
            return

        msg = await ACTION_MANAGER.add_action(interaction.guild.id, action_str, translator=self.bot.translate)
        await interaction.followup.send(msg, ephemeral=True)

    @ai_mod_group.command(
        name="remove_action",
        description=locale_string("cogs.autonomous_moderation.meta.remove_action.description"),
    )
    @app_commands.describe(
        action=locale_string("cogs.autonomous_moderation.meta.remove_action.action")
    )
    @app_commands.autocomplete(action=ACTION_MANAGER.autocomplete)
    async def remove_action(self, interaction: Interaction, action: str):
        if not await can_run(interaction):
            return
        msg = await ACTION_MANAGER.remove_action(interaction.guild.id, action, translator=self.bot.translate)
        await interaction.response.send_message(msg, ephemeral=True)

    @ai_mod_group.command(
        name="toggle",
        description=locale_string("cogs.autonomous_moderation.meta.toggle.description"),
    )
    @app_commands.describe(
        enable=locale_string("cogs.autonomous_moderation.meta.toggle.enable"),
        mode=locale_string("cogs.autonomous_moderation.meta.toggle.mode"),
    )
    @app_commands.choices(
        enable=[
            app_commands.Choice(
                name=locale_string("cogs.autonomous_moderation.meta.toggle.enable_choice"),
                value="enable",
            ),
            app_commands.Choice(
                name=locale_string("cogs.autonomous_moderation.meta.toggle.disable_choice"),
                value="disable",
            ),
            app_commands.Choice(
                name=locale_string("cogs.autonomous_moderation.meta.toggle.status_choice"),
                value="status",
            ),
        ],
        mode=[
            app_commands.Choice(
                name=locale_string("cogs.autonomous_moderation.meta.toggle.mode_choices.report"),
                value="report",
            ),
            app_commands.Choice(
                name=locale_string("cogs.autonomous_moderation.meta.toggle.mode_choices.interval"),
                value="interval",
            ),
            app_commands.Choice(
                name=locale_string("cogs.autonomous_moderation.meta.toggle.mode_choices.adaptive"),
                value="adaptive",
            ),
        ],
    )
    async def toggle(self, interaction: Interaction, enable: app_commands.Choice[str], mode: app_commands.Choice[str] | None = None):
        if not await can_run(interaction):
            return
        gid = interaction.guild.id
        toggle_texts = self.bot.translate("cogs.autonomous_moderation.toggle",
                                           guild_id=gid)

        if enable.value == "status":
            enabled = await mysql.get_settings(gid, "autonomous-mod")
            current_mode = await mysql.get_settings(gid, "aimod-mode") or "report"
            state_label = toggle_texts["state_enabled"] if enabled else toggle_texts["state_disabled"]
            mode_label = toggle_texts.get("modes", {}).get(current_mode, current_mode)
            msg = toggle_texts["status"].format(state=state_label, mode=mode_label)

            if current_mode == "adaptive":
                active = await mysql.get_settings(gid, "aimod-active-mode") or "report"
                active_label = toggle_texts.get("modes", {}).get(active, active)
                msg += toggle_texts["status_active"].format(active=active_label)

            await interaction.response.send_message(msg, ephemeral=True)
            return

        if enable.value == "enable":
            rules = await mysql.get_settings(gid, "rules")
            if not rules:
                await interaction.response.send_message(toggle_texts["need_rules"], ephemeral=True)
                return
            await mysql.update_settings(gid, "autonomous-mod", True)
            status_msg = toggle_texts["enabled"]
        else:
            await mysql.update_settings(gid, "autonomous-mod", False)
            status_msg = toggle_texts["disabled"]

        if mode:
            await mysql.update_settings(gid, "aimod-mode", mode.value)
            mode_label = toggle_texts.get("modes", {}).get(mode.value, mode.value)
            status_msg += "\n" + toggle_texts["mode_set"].format(mode=mode_label)

        await interaction.response.send_message(status_msg, ephemeral=True)

    @ai_mod_group.command(
        name="view_actions",
        description=locale_string("cogs.autonomous_moderation.meta.view_actions.description"),
    )
    async def view_actions(self, interaction: Interaction):
        if not await can_run(interaction):
            return
        actions = await ACTION_MANAGER.view_actions(interaction.guild.id)
        guild_id = interaction.guild.id
        actions_texts = self.bot.translate("cogs.autonomous_moderation.actions",
                                           guild_id=guild_id)
        if not actions:
            await interaction.response.send_message(actions_texts["none"], ephemeral=True)
            return

        formatted = "\n".join(f"{i + 1}. `{a}`" for i, a in enumerate(actions))
        await interaction.response.send_message(
            actions_texts["heading"].format(actions=formatted),
            ephemeral=True,
        )

    @ai_mod_group.command(
        name="add_adaptive_event",
        description=locale_string("cogs.autonomous_moderation.meta.add_adaptive_event.description"),
    )
    @app_commands.describe(
        role=locale_string("cogs.autonomous_moderation.meta.add_adaptive_event.role"),
        channel=locale_string("cogs.autonomous_moderation.meta.add_adaptive_event.channel"),
        time_range=locale_string("cogs.autonomous_moderation.meta.add_adaptive_event.time_range"),
        threshold=locale_string("cogs.autonomous_moderation.meta.add_adaptive_event.threshold"),
    )
    @app_commands.choices(
        event=[
            app_commands.Choice(
                name=locale_string(key, default=label),
                value=value,
            )
            for label, value, key in ADAPTIVE_EVENTS
        ],
        action=[
            app_commands.Choice(
                name=locale_string(key, default=label),
                value=value,
            )
            for label, value, key in ACTIONS
        ]
    )
    async def add_adaptive_event(
        self,
        interaction: Interaction,
        event: app_commands.Choice[str],
        action: app_commands.Choice[str],
        role: discord.Role = None,
        channel: discord.TextChannel = None,
        time_range: str = None,
        threshold: float = None
    ):
        if not await can_run(interaction):
            return
        if not await ensure_adaptive_mode(interaction):
            return

        guild_id = interaction.guild.id
        adaptive_texts = self.bot.translate("cogs.autonomous_moderation.adaptive",
                                            guild_id=guild_id)

        try:
            if event.value in {"role_online", "role_offline"}:
                if not role:
                    await interaction.response.send_message(adaptive_texts["role_required"], ephemeral=True)
                    return
                event_key = f"{event.value}:{role.id}"

            elif event.value == "role_online_percent":
                if not role:
                    await interaction.response.send_message(adaptive_texts["role_required"], ephemeral=True)
                    return
                if threshold is None:
                    await interaction.response.send_message(adaptive_texts["threshold_required"], ephemeral=True)
                    return
                if not (0 < threshold <= 1):
                    await interaction.response.send_message(adaptive_texts["threshold_range"], ephemeral=True)
                    return
                event_key = f"{event.value}:{role.id}:{threshold}"

            elif event.value == "time_range":
                if not time_range:
                    await interaction.response.send_message(adaptive_texts["time_range_required"], ephemeral=True)
                    return
                event_key = f"time_range:{time_range}"

            else:
                event_key = event.value

            msg = await EVENT_MANAGER.add_event(interaction.guild.id, event_key, action.value, translator=self.bot.translate)
            await interaction.response.send_message(msg, ephemeral=True)

        except Exception as exc:
            await interaction.response.send_message(
                adaptive_texts["error"].format(error=exc),
                ephemeral=True,
            )

    @ai_mod_group.command(
        name="remove_adaptive_event",
        description=locale_string("cogs.autonomous_moderation.meta.remove_adaptive_event.description"),
    )
    @app_commands.describe(
        event_key=locale_string("cogs.autonomous_moderation.meta.remove_adaptive_event.event_key"),
        action=locale_string("cogs.autonomous_moderation.meta.remove_adaptive_event.action"),
    )
    @app_commands.autocomplete(event_key=EVENT_MANAGER.autocomplete_event)
    @app_commands.choices(
        action=[
            app_commands.Choice(
                name=locale_string(key, default=label),
                value=value,
            )
            for label, value, key in ACTIONS
        ]
    )
    async def remove_adaptive_event(self, interaction: Interaction, event_key: str, action: app_commands.Choice[str]):
        if not await can_run(interaction):
            return
        if not await ensure_adaptive_mode(interaction):
            return
        msg = await EVENT_MANAGER.remove_event_action(interaction.guild.id, event_key, action.value, translator=self.bot.translate)
        await interaction.response.send_message(msg, ephemeral=True)

    @ai_mod_group.command(
        name="clear_adaptive_events",
        description=locale_string("cogs.autonomous_moderation.meta.clear_adaptive_events.description"),
    )
    async def clear_adaptive_events(self, interaction: Interaction):
        if not await can_run(interaction):
            return
        if not await ensure_adaptive_mode(interaction):
            return
        guild_id = interaction.guild.id
        adaptive_texts = self.bot.translate("cogs.autonomous_moderation.adaptive",
                                            guild_id=guild_id)
        await mysql.update_settings(interaction.guild.id, AIMOD_EVENT_SETTING, {})
        await interaction.response.send_message(adaptive_texts["cleared"], ephemeral=True)

    @ai_mod_group.command(
        name="view_adaptive_events",
        description=locale_string("cogs.autonomous_moderation.meta.view_adaptive_events.description"),
    )
    async def view_adaptive_events(self, interaction: Interaction):
        if not await can_run(interaction):
            return
        if not await ensure_adaptive_mode(interaction):
            return
        guild_id = interaction.guild.id
        adaptive_texts = self.bot.translate("cogs.autonomous_moderation.adaptive",
                                            guild_id=guild_id)
        settings = await EVENT_MANAGER.view_events(interaction.guild.id)
        if not settings:
            await interaction.response.send_message(adaptive_texts["none"], ephemeral=True)
            return

        grouped: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        for key, actions in settings.items():
            if ":" in key:
                prefix, detail = key.split(":", 1)
            else:
                prefix, detail = key, ""
            for action in actions:
                grouped[prefix][action].append(detail)

        lines: list[str] = []
        for event, action_map in grouped.items():
            event_key = VALID_ADAPTIVE_EVENTS.get(event)
            if event_key:
                event_label = self.bot.translate(event_key, guild_id=guild_id)
            else:
                event_label = event
            lines.append(adaptive_texts["section"].format(event=event_label))
            for action, details in action_map.items():
                detail_strings: list[str] = []
                for val in details:
                    if event in {"role_online", "role_offline"}:
                        try:
                            role = interaction.guild.get_role(int(val))
                            detail_strings.append(role.mention if role else adaptive_texts["detail_role"].format(id=val))
                        except ValueError:
                            detail_strings.append(adaptive_texts["detail_invalid_role"].format(value=val))
                    elif event == "role_online_percent":
                        try:
                            role_id, threshold_val = val.split(":")
                            role = interaction.guild.get_role(int(role_id))
                            display = role.mention if role else adaptive_texts["detail_role"].format(id=role_id)
                            detail_strings.append(adaptive_texts["detail_threshold"].format(display=display, threshold=threshold_val))
                        except Exception:
                            detail_strings.append(adaptive_texts["detail_invalid"].format(value=val))
                    elif event == "time_range":
                        detail_strings.append(adaptive_texts["detail_time"].format(value=val))
                    elif val:
                        detail_strings.append(adaptive_texts["detail_time"].format(value=val))
                if detail_strings:
                    detail_text = ", ".join(detail_strings)
                    details_suffix = adaptive_texts["detail_join"].format(details=detail_text)
                else:
                    details_suffix = ""
                action_key = ACTION_LABEL_KEYS.get(action)
                action_label = (
                    self.bot.translate(action_key, guild_id=guild_id)
                    if action_key
                    else action
                )
                lines.append(adaptive_texts["item"].format(action=action_label, details=details_suffix))

        await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def setup_commands(bot: commands.Bot):
    await bot.add_cog(AutonomousCommandsCog(bot))


async def ensure_adaptive_mode(interaction: Interaction) -> bool:
    mode = await mysql.get_settings(interaction.guild.id, "aimod-mode")
    if mode != "adaptive":
        await interaction.response.send_message(
            _translate(interaction, "cogs.autonomous_moderation.ensure_adaptive"),
            ephemeral=True,
        )
        return False
    return True