import re
import json
import openai
import discord
from discord.ext import commands
from discord import app_commands, Interaction
from typing import Optional
from collections import defaultdict, deque

from modules.utils import mysql, logging
from modules.moderation import strike
from modules.utils.action_manager import ActionListManager
from modules.utils.actions import VALID_ACTION_VALUES, action_choices
from modules.utils.strike import validate_action
from cogs.banned_words import normalize_text

TIME_RE = re.compile(r"timeout:(\d+)([smhdw])$")
ALLOWED_SIMPLE = {"strike", "kick", "ban", "delete", "auto"}
ALLOWED_ACTIONS = ALLOWED_SIMPLE | {"timeout"}

AIMOD_ACTION_SETTING = "aimod-detection-action"
manager = ActionListManager(AIMOD_ACTION_SETTING)

violation_cache: dict[int, deque[tuple[str, str]]] = defaultdict(lambda: deque(maxlen=3))

def valid_timeout(action: str) -> bool:
    return bool(TIME_RE.fullmatch(action))

def parse_ai_response(text: str) -> tuple[list[str], str, str, bool]:
    try:
        data = json.loads(text)
    except Exception:
        return [], "", "", False
    ok = bool(data.get("ok"))
    actions = [a.lower() for a in data.get("actions", []) if a] if not ok else []
    return actions, data.get("rule", ""), data.get("reason", ""), ok

async def moderate_event(
    bot,
    guild: discord.Guild,
    user: discord.User,
    event_type: str,
    content: str,
    message_obj: Optional[discord.Message] = None
):
    if await mysql.get_settings(guild.id, "autonomous-mod") is not True:
        return
    api_key = await mysql.get_settings(guild.id, "api-key")
    if not api_key:
        return
    rules = await mysql.get_settings(guild.id, "rules")
    if not rules:
        return
    normalized_message = normalize_text(content)
    if not normalized_message:
        return

    history = list(violation_cache[user.id])
    past_text = ""
    if history:
        past_text = "\n\nPrevious Violations:\n" + "\n".join(
            f"- {i}. Rule: {r} | Reason: {t}" for i, (r, t) in enumerate(history, 1)
        )

    client = openai.AsyncOpenAI(api_key=api_key)
    try:
        completion = await client.chat.completions.create(
            model=await mysql.get_settings(guild.id, "aimod-model") or "gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an AI moderator for a Discord server.\n"
                        "Only flag messages that clearly, directly, and unambiguously violate a rule.\n"
                        "Ignore tone, sarcasm, rudeness, slang, memes, or opinions unless a rule is explicitly broken.\n"
                        "Never guess intent or infer meaning — if a violation isn't obvious, return ok=true.\n\n"
                        f"Server rules:\n{rules}\n"
                        f"{past_text if history else 'This user has no prior violations.'}\n\n"
                        "If no rule is clearly broken, return ok=true.\n\n"
                        "Respond in strict JSON:\n"
                        "- ok (bool): true if no rule was broken\n"
                        "- rule (string): name of the rule broken\n"
                        "- reason (string): short explanation\n"
                        "- actions (array): any of ['delete', 'strike', 'kick', 'ban', 'timeout:<duration>', 'warn:<warning>']\n\n"
                        "Valid durations: 1s, 1m, 1h, 1d, 1w, 1mo, 1y\n\n"
                        "Use actions proportionately and escalate based on user history:\n"
                        "- warn: notify the user with a public warning message about their behavior.\n"
                        "- delete: minor issues needing only content removal\n"
                        "- timeout: low to moderate rule breaks\n"
                        "- strike: serious or harmful behavior, or repeat violations (strikes are permanent and may lead to a ban)\n"
                        "- kick: repeated serious rule-breaking or aggressive conduct\n"
                        "- ban: ongoing severe violations, or multiple prior offenses with no improvement\n"
                    )
                },
                {
                    "role": "user",
                    "content": f"Evaluate the following message from a user:\n\n\"{normalized_message}\"\n\nEvent type: {event_type}"
                }
            ],
            temperature=0.0,
            response_format={"type": "json_object"}
        )
    except Exception:
        return

    raw = completion.choices[0].message.content.strip()
    actions_from_ai, rule_broken, reason_text, ok_flag = parse_ai_response(raw)
    if ok_flag or not actions_from_ai:
        return

    if rule_broken and reason_text:
        violation_cache[user.id].appendleft((rule_broken, reason_text))

    embed = discord.Embed(
        title="AI-Flagged Violation",
        description=(
            f"Event: {event_type}\nUser: {user.mention} ({user.name})\n"
            f"Rule Broken: {rule_broken}\nReason: {reason_text}\n"
            f"Actions: {', '.join(actions_from_ai)}"
        ),
        colour=discord.Colour.red()
    )
    monitor_channel = await mysql.get_settings(guild.id, "monitor-channel")
    if monitor_channel:
        await logging.log_to_channel(embed, monitor_channel, bot)

    configured = await mysql.get_settings(guild.id, "aimod-detection-action") or ["auto"]
    if "auto" in configured:
        configured = actions_from_ai

    for act in configured:
        await strike.perform_disciplinary_action(
            user=user,
            bot=bot,
            action_string=act.lower(),
            reason=reason_text,
            source="autonomous_ai",
            message=message_obj
        )

class AutonomousModeratorCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def handle_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        await moderate_event(self.bot, message.guild, message.author, "Message", message.content, message)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if after.author.bot or not after.guild or before.content == after.content:
            return
        await moderate_event(self.bot, after.guild, after.author, "Edited Message",
                             f"Before: {before.content}\nAfter: {after.content}", after)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        await moderate_event(self.bot, member.guild, member, "Member Join",
                             f"Username: {member.name}, Display: {member.display_name}")

    ai_mod_group = app_commands.Group(name="ai_mod", description="Manage AI moderation features.")

    @ai_mod_group.command(name="rules_set", description="Set server rules")
    @app_commands.default_permissions(manage_guild=True)
    async def set_rules(this, interaction: Interaction, *, rules: str):
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

        msg = await manager.add_action(interaction.guild.id, action_str)
        await interaction.followup.send(msg, ephemeral=True)

    @ai_mod_group.command(name="remove_action", description="Remove a specific action from the list of punishments.")
    @app_commands.describe(action="Exact action string to remove (e.g. timeout, delete)")
    async def remove_action(self, interaction: Interaction, action: str):
        msg = await manager.remove_action(interaction.guild.id, action)
        await interaction.response.send_message(msg, ephemeral=True)
    async def remove_action_autocomplete(
        interaction: Interaction,
        current: str
    ) -> list[app_commands.Choice[str]]:
        actions = await manager.view_actions(interaction.guild.id)
        return [
            app_commands.Choice(name=action, value=action)
            for action in actions if current.lower() in action.lower()
        ][:25]

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
        actions = await manager.view_actions(interaction.guild.id)
        if not actions:
            await interaction.response.send_message("No actions are currently set.", ephemeral=True)
            return

        formatted = "\n".join(f"{i+1}. `{a}`" for i, a in enumerate(actions))
        await interaction.response.send_message(
            f"**Current actions:**\n{formatted}",
            ephemeral=True
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(AutonomousModeratorCog(bot))