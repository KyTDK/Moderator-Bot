import discord
from discord.ext import commands
from discord import app_commands, Interaction
from collections import defaultdict

from modules.utils import mysql
from modules.utils.event_manager import EventListManager
from modules.utils.action_manager import ActionListManager
from modules.utils.actions import VALID_ACTION_VALUES, action_choices
from modules.utils.strike import validate_action


AIMOD_ACTION_SETTING = "aimod-detection-action"
ACTION_MANAGER = ActionListManager(AIMOD_ACTION_SETTING)

ADAPTIVE_EVENTS = [
    ("Role Online", "role_online"),
    ("Role Offline", "role_offline"),
    ("Mass Join", "mass_join"),
    ("Mass Leave", "mass_leave"),
    ("Channel Spike", "channel_spike"),
    ("Server Inactivity", "guild_inactive"),
    ("Role Online %", "role_online_percent"),
    ("Time Range", "time_range"),
    ("Server Spike", "server_spike")
]
VALID_ADAPTIVE_EVENTS = {value: label for label, value in ADAPTIVE_EVENTS}
ACTIONS = [
    ("Enable Interval Mode", "enable_interval"),
    ("Disable Interval Mode", "disable_interval"),
    ("Enable Report Mode", "enable_report"),
    ("Disable Report Mode", "disable_report")
]

AIMOD_EVENT_SETTING = "aimod-adaptive-events"
EVENT_MANAGER = EventListManager(AIMOD_EVENT_SETTING)

class AutonomousCommandsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    ai_mod_group = app_commands.Group(name="ai_mod", 
                                      description="Manage AI moderation features.",
                                      default_permissions=discord.Permissions(manage_messages=True),
                                      guild_only=True)

    @ai_mod_group.command(name="rules_set", description="Set server rules")
    @app_commands.default_permissions(manage_guild=True)
    async def set_rules(this, interaction: Interaction, *, rules: str):
        if not await mysql.get_settings(interaction.guild.id, "api-key"):
            await interaction.response.send_message("Set an API key first with `/settings set api-key`.", ephemeral=True)
            return
        await mysql.update_settings(interaction.guild.id, "rules", rules)
        await interaction.response.send_message("Rules updated.", ephemeral=True)

    @ai_mod_group.command(name="rules_show", description="Show the currently configured moderation rules.")
    async def show_rules(self, interaction: Interaction):
        rules = await mysql.get_settings(interaction.guild.id, "rules")
        if not rules:
            await interaction.response.send_message("No moderation rules have been set.", ephemeral=True)
            return

        if len(rules) > 1900:
            rules = rules[:1900] + "…"

        await interaction.response.send_message(f"**Moderation Rules:**\n```{rules}```", ephemeral=True)

    @ai_mod_group.command(name="add_action", description="Add enforcement actions")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.choices(action=action_choices(include=[("Auto", "auto")]))
    async def add_action(self,
            interaction: Interaction,
            action: str,
            duration: str = None,
            role: discord.Role = None,
            reason: str = None,
        ):
        await interaction.response.defer(ephemeral=True)

        if not await mysql.get_settings(interaction.guild.id, "api-key"):
            await interaction.response.send_message("Set an API key first with `/settings set api-key`.", ephemeral=True)
            return

        action_str = await validate_action(
            interaction=interaction,
            action=action,
            duration=duration,
            role=role,
            valid_actions = VALID_ACTION_VALUES + ["auto"],
            param=reason,
        )
        if action_str is None:
            return

        msg = await ACTION_MANAGER.add_action(interaction.guild.id, action_str)
        await interaction.followup.send(msg, ephemeral=True)

    @ai_mod_group.command(name="remove_action", description="Remove a specific action from the list of punishments.")
    @app_commands.describe(action="Exact action string to remove (e.g. timeout, delete)")
    @app_commands.autocomplete(action=ACTION_MANAGER.autocomplete)
    async def remove_action(self, interaction: Interaction, action: str):
        msg = await ACTION_MANAGER.remove_action(interaction.guild.id, action)
        await interaction.response.send_message(msg, ephemeral=True)

    @ai_mod_group.command(name="toggle", description="Enable, disable, or view status of AI moderation")
    @app_commands.describe(action="enable | disable | status")
    @app_commands.choices(
        action=[
            app_commands.Choice(name="enable",  value="enable"),
            app_commands.Choice(name="disable", value="disable"),
            app_commands.Choice(name="status",  value="status"),
        ]
    )
    @app_commands.default_permissions(manage_guild=True)
    async def toggle_autonomous(self, interaction: Interaction, action: app_commands.Choice[str]):
        gid = interaction.guild.id

        if action.value == "status":
            enabled = await mysql.get_settings(gid, "autonomous-mod")
            await interaction.response.send_message(
                f"Autonomous moderation is **{'enabled' if enabled else 'disabled'}**.", ephemeral=True
            )
            return

        if action.value == "enable":
            key = await mysql.get_settings(gid, "api-key")
            rules = await mysql.get_settings(gid, "rules")
            if not key:
                await interaction.response.send_message("Set an API key first with `/settings set api-key`.", ephemeral=True)
                return
            if not rules:
                await interaction.response.send_message("Set moderation rules first with `/ai_mod rules_set`.", ephemeral=True)
                return

        await mysql.update_settings(gid, "autonomous-mod", action.value == "enable")
        await interaction.response.send_message(
            f"Autonomous moderation **{action.value}d**.", ephemeral=True
        )

    @ai_mod_group.command(name="view_actions", description="Show all actions currently configured to trigger when the AI detects a violation.")
    async def view_actions(self, interaction: Interaction):
        actions = await ACTION_MANAGER.view_actions(interaction.guild.id)
        if not actions:
            await interaction.response.send_message("No actions are currently set.", ephemeral=True)
            return

        formatted = "\n".join(f"{i+1}. `{a}`" for i, a in enumerate(actions))
        await interaction.response.send_message(
            f"**Current actions:**\n{formatted}",
            ephemeral=True
        )

    @ai_mod_group.command(name="set_mode", description="Manually set the AI moderation mode (report, interval, or adaptive).")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="Report Mode", value="report"),
            app_commands.Choice(name="Interval Mode", value="interval"),
            app_commands.Choice(name="Adaptive Mode", value="adaptive")
        ]
    )
    @app_commands.default_permissions(manage_guild=True)
    async def set_mode(self, interaction: Interaction, mode: app_commands.Choice[str]):
        await mysql.update_settings(interaction.guild.id, "aimod-mode", mode.value)
        await interaction.response.send_message(
            f"AI moderation mode has been set to **{mode.value}**.", ephemeral=True
        )

    @ai_mod_group.command(name="add_adaptive_event", description="Link a trigger event to one or more moderation mode actions.")
    @app_commands.describe(
        role="Which roles to monitor (if applicable)",
        channel="Which channel to monitor (for spike events)",
        time_range="Time range in HH:MM-HH:MM for time-based triggers",
        threshold="Threshold for role online percent (0 to 1, optional)"
    )
    @app_commands.choices(
        event=[app_commands.Choice(name=label, value=value) for label, value in ADAPTIVE_EVENTS],
        action=[app_commands.Choice(name=label, value=value) for label, value in ACTIONS]
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
        if not await ensure_adaptive_mode(interaction):
            return

        try:
            if event.value in {"role_online", "role_offline"}:
                if not role:
                    await interaction.response.send_message("This event type requires a role.", ephemeral=True)
                    return
                event_key = f"{event.value}:{role.id}"

            elif event.value == "role_online_percent":
                if not role:
                    await interaction.response.send_message("This event type requires a role.", ephemeral=True)
                    return
                if not threshold:
                    await interaction.response.send_message("This event type requires a threshold.", ephemeral=True)
                    return
                if not (0 < threshold <= 1):
                    await interaction.response.send_message("Threshold must be a value between 0 and 1.", ephemeral=True)
                    return
                event_key = f"{event.value}:{role.id}:{threshold}"

            elif event.value == "channel_spike":
                if not channel:
                    await interaction.response.send_message("This event type requires a channel.", ephemeral=True)
                    return
                event_key = f"channel_spike:{channel.id}"

            elif event.value == "time_range":
                if not time_range:
                    await interaction.response.send_message("This event type requires a time range (e.g., 08:00-20:00).", ephemeral=True)
                    return
                event_key = f"time_range:{time_range}"

            else:
                event_key = event.value

            msg = await EVENT_MANAGER.add_event(interaction.guild.id, event_key, action.value)
            await interaction.response.send_message(msg, ephemeral=True)

        except Exception as e:
            await interaction.response.send_message(f"Error setting event: {e}", ephemeral=True)

    @ai_mod_group.command(name="remove_adaptive_event", description="Remove a specific action from a configured event.")
    @app_commands.describe(event_key="Adaptive event key (autocompletes)", action="Action to remove from this event")
    @app_commands.autocomplete(event_key=EVENT_MANAGER.autocomplete_event)
    @app_commands.choices(action=[app_commands.Choice(name=label, value=value) for label, value in ACTIONS])
    async def remove_adaptive_event(self, interaction: Interaction, event_key: str, action: app_commands.Choice[str]):
        if not await ensure_adaptive_mode(interaction):
            return
        msg = await EVENT_MANAGER.remove_event_action(interaction.guild.id, event_key, action.value)
        await interaction.response.send_message(msg, ephemeral=True)

    @ai_mod_group.command(name="clear_adaptive_events", description="Clear all adaptive event triggers.")
    async def clear_adaptive_events(self, interaction: Interaction):
        if not await ensure_adaptive_mode(interaction):
            return
        await mysql.update_settings(interaction.guild.id, AIMOD_EVENT_SETTING, {})
        await interaction.response.send_message("All adaptive events have been cleared.", ephemeral=True)

    @ai_mod_group.command(name="view_adaptive_events", description="Show current adaptive moderation triggers.")
    async def view_adaptive_events(self, interaction: Interaction):
        if not await ensure_adaptive_mode(interaction):
            return

        settings = await EVENT_MANAGER.view_events(interaction.guild.id)
        if not settings:
            await interaction.response.send_message("No adaptive events set.", ephemeral=True)
            return

        grouped: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        for key, actions in settings.items():
            if ":" in key:
                prefix, detail = key.split(":", 1)
            else:
                prefix, detail = key, ""
            for action in actions:
                grouped[prefix][action].append(detail)

        lines = []
        for event, action_map in grouped.items():
            event_label = VALID_ADAPTIVE_EVENTS.get(event, event)
            lines.append(f"**{event_label}**")
            for action, details in action_map.items():
                mentions = []
                for val in details:
                    if event == "channel_spike":
                        try:
                            ch = interaction.guild.get_channel(int(val))
                            mentions.append(ch.mention if ch else f"`channel_id={val}`")
                        except ValueError:
                            mentions.append(f"[invalid channel: {val}]")
                    elif event in {"role_online", "role_offline"}:
                        try:
                            role = interaction.guild.get_role(int(val))
                            mentions.append(role.mention if role else f"`role_id={val}`")
                        except ValueError:
                            mentions.append(f"[invalid role: {val}]")
                    elif event == "role_online_percent":
                        try:
                            role_id, threshold = val.split(":")
                            role = interaction.guild.get_role(int(role_id))
                            display = role.mention if role else f"`role_id={role_id}`"
                            mentions.append(f"{display} ≥ `{threshold}`")
                        except Exception:
                            mentions.append(f"[invalid: {val}]")
                    elif event == "time_range":
                        mentions.append(f"`{val}`")
                    elif val:
                        mentions.append(f"`{val}`")
                joined = f" → {', '.join(mentions)}" if mentions else ""
                lines.append(f"• `{action}`{joined}")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

async def setup_commands(bot: commands.Bot):
    await bot.add_cog(AutonomousCommandsCog(bot))

async def ensure_adaptive_mode(interaction: Interaction) -> bool:
    mode = await mysql.get_settings(interaction.guild.id, "aimod-mode")
    if mode != "adaptive":
        await interaction.response.send_message(
            "Adaptive moderation mode is not enabled. Use `/ai_mod set_mode` to enable it.",
            ephemeral=True
        )
        return False
    return True