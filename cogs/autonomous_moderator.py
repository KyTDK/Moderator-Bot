import re
import json
import openai
import discord
from discord.ext import commands
from discord import app_commands, Interaction
from typing import Optional

from modules.utils import mysql, logging
from modules.moderation import strike

TIME_RE = re.compile(r"timeout:(\d+)([smhdw])$")
ALLOWED_SIMPLE = {"strike", "kick", "ban", "delete", "auto"}
ALLOWED_ACTIONS = ALLOWED_SIMPLE | {"timeout"}


def valid_timeout(action: str) -> bool:
    return bool(TIME_RE.fullmatch(action))

def parse_ai_response(text: str) -> tuple[list[str], str, str, bool]:
    try:
        data = json.loads(text)
    except Exception:
        return [], "", "", False
    ok = bool(data.get("ok"))
    actions = [a.lower() for a in data.get("actions", []) if a] if not ok else []
    rule = data.get("rule", "")
    reason = data.get("reason", "")
    return actions, rule, reason, ok

class AutonomousModeratorCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

ai_mod_group = app_commands.Group(name="ai_mod", description="Manage AI moderation features.")

@ai_mod_group.command(name="rules_set", description="Set server rules")
@app_commands.default_permissions(manage_guild=True)
async def set_rules(interaction: Interaction, *, rules: str):
    await mysql.update_settings(interaction.guild.id, "rules", rules)
    await interaction.response.send_message("Rules updated.", ephemeral=True)

@ai_mod_group.command(name="set_action", description="Set enforcement actions")
@app_commands.default_permissions(manage_guild=True)
async def set_action(interaction: Interaction, *, actions: str):
    chosen = [a.strip().lower() for a in actions.split(",") if a.strip()]
    invalid = [a for a in chosen if a not in ALLOWED_SIMPLE and not valid_timeout(a)]
    if invalid:
        await interaction.response.send_message("Invalid: " + ", ".join(invalid), ephemeral=True)
        return
    await mysql.update_settings(interaction.guild.id, "aimod-detection-action", chosen)
    await interaction.response.send_message("Actions set: " + ", ".join(chosen), ephemeral=True)

@ai_mod_group.command(name="toggle", description="Enable or disable AI moderation")
@app_commands.default_permissions(manage_guild=True)
async def toggle_autonomous(interaction: Interaction, enabled: bool):
    key = await mysql.get_settings(interaction.guild.id, "api-key")
    rules = await mysql.get_settings(interaction.guild.id, "rules")

    if enabled:
        if not key:
            await interaction.response.send_message("Set an API key first with /settings set api-key.", ephemeral=True)
            return
        if not rules:
            await interaction.response.send_message("Set moderation rules first with /ai_mod rules_set.", ephemeral=True)
            return

    await mysql.update_settings(interaction.guild.id, "autonomous-mod", enabled)
    await interaction.response.send_message(
        f"Autonomous moderation {'enabled' if enabled else 'disabled'}.",
        ephemeral=True
    )

@ai_mod_group.command(name="status", description="Show current AI mod settings")
async def status(interaction: Interaction):
    gid = interaction.guild.id
    enabled = await mysql.get_settings(gid, "autonomous-mod")
    actions = await mysql.get_settings(gid, "aimod-detection-action") or ["auto"]
    await interaction.response.send_message(f"Enabled: {enabled}\nActions: {', '.join(actions)}", ephemeral=True)

async def moderate_event(bot, guild: discord.Guild, user: discord.User, event_type: str, content: str,
                         message_obj: Optional[discord.Message] = None):
    if await mysql.get_settings(guild.id, "autonomous-mod") is not True:
        return
    api_key = await mysql.get_settings(guild.id, "api-key")
    if not api_key:
        return
    rules = await mysql.get_settings(guild.id, "rules")
    if not rules:
        return
    client = openai.AsyncOpenAI(api_key=api_key)
    try:
        completion = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an AI moderator for a Discord server.\n"
                        "Server rules:\n"
                        f"{rules}\n\n"
                        "Evaluate the user behaviour and decide if any rule is broken.\n"
                        "Return JSON with keys: ok (bool), rule (string), reason (string), actions (array of strings).\n"
                        "Allowed actions: strike, kick, ban, delete, timeout:<dur>.\n"
                        "Only issue proportionate punishments. For no violation, return ok=true.\n"
                        "Strike is a serious punishment. Use it only if the behavior is harmful or repeated.\n"
                        "Timeouts (like timeout:5m or timeout:1h) should be used for most minor issues.\n"
                        "Use 'delete' if the message should be removed but no punishment is needed.\n"
                        "'Ban' is the most severe and should only be used for extreme or repeated violations.\n"
                    )
                },
                {
                    "role": "user",
                    "content": f"Event: {event_type}\nUser Action:\n{content}"
                }
            ],
            temperature=0.0,
            response_format={"type": "json_object"}
        )
    except Exception:
        return
    raw_response = completion.choices[0].message.content.strip()
    actions_from_ai, rule_broken, reason_text, ok_flag = parse_ai_response(raw_response)
    if ok_flag or not actions_from_ai:
        print(f"{ok_flag} and {actions_from_ai}")
        return

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
        act = act.lower()
        if act == "delete" and message_obj:
            try:
                await message_obj.delete()
            except Exception:
                pass
            continue
        await strike.perform_disciplinary_action(
            user=user,
            bot=bot,
            action_string=act,
            reason="AI-flagged violation",
            source="autonomous_ai"
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

    async def cog_load(self):
        self.bot.tree.add_command(ai_mod_group)

async def setup(bot: commands.Bot):
    await bot.add_cog(AutonomousModeratorCog(bot))