import json
import openai
import discord
from datetime import datetime, timedelta, timezone
from discord.ext import commands, tasks
from discord import app_commands, Interaction
from collections import defaultdict

from modules.utils import logging, mysql
from modules.utils.time import parse_duration
from modules.moderation import strike
from modules.utils.action_manager import ActionListManager
from modules.utils.actions import VALID_ACTION_VALUES, action_choices
from modules.utils.strike import validate_action

from cogs.banned_words import normalize_text

from math import ceil

AIMOD_ACTION_SETTING = "aimod-detection-action"
manager = ActionListManager(AIMOD_ACTION_SETTING)

SYSTEM_MSG = (
    "You are an AI that checks whether messages violate server rules.\n"
    "Respond with a JSON object containing a `results` field.\n"
    "`results` must be an array of violations. Each violation must include:\n"
    "- user_id (string)\n"
    "- rule (string)\n"
    "- reason (string)\n"
    "- actions (array of strings, e.g. ['strike', 'timeout:1h', 'delete'])\n"
    "- message_ids (optional array of message IDs to delete)\n\n"
    "If any message_ids are listed, always include 'delete' in the actions array.\n"
    "Valid actions: delete, strike, kick, ban, timeout:<duration>, warn:<text>.\n\n"
    "Punishment meanings:\n"
    "- warn:<text>: Gentle notice for low-risk or borderline behavior.\n"
    "- delete: Always include this for rule-breaking messages to remove them from chat.\n"
    "- timeout:<duration>: Temporary mute for moderate issues.\n"
    "- kick: Remove user from server (temporary).\n"
    "- strike: Serious and permanent. Only for clear, major rule violations.\n"
    "- ban: Permanent removal for extreme or repeated abuse.\n\n"
    "Only enforce the server rules provided. Do not apply personal judgment, intent, or external policies (e.g., OpenAI guidelines).\n"
    "Do not flag messages that report, reference, or accuse others of breaking rules — only flag content where the speaker themselves is clearly breaking a rule.\n"
    "Do not act on sarcasm, vague statements, or suggestive content unless it clearly and unambiguously breaks a rule.\n"
    "Never speculate — if unsure, err on the side of ok=true.\n"
)

def estimate_tokens(text: str) -> int:
    return ceil(len(text) / 4)

MODEL_LIMITS = {
    "gpt-4.1": 1000000,
    "gpt-4.1-nano": 1000000,
    "gpt-4.1-mini": 1000000,
    "gpt-4o": 128000,
    "gpt-3.5-turbo": 16000
}

def get_model_limit(model_name: str) -> int:
    for key, limit in MODEL_LIMITS.items():
        if key in model_name:
            return limit
    return 16000  # safe fallback

def parse_batch_response(text: str) -> list[dict[str, object]]:
    try:
        data = json.loads(text)
        data = data.get("results", data)
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            return []

        parsed = []
        for item in data:
            if not isinstance(item, dict):
                continue
            try:
                uid = int(item.get("user_id"))
                actions = item.get("actions") or item.get("action") or []
                if isinstance(actions, str):
                    actions = [actions]
                parsed.append({
                    "user_id": uid,
                    "rule": str(item.get("rule", "")),
                    "reason": str(item.get("reason", "")),
                    "actions": [a.lower() for a in actions if isinstance(a, str)],
                    "message_ids": item.get("message_ids", [])
                })
            except Exception:
                continue
        return parsed
    except Exception as e:
        print(f"[parse_batch_response] Failed to parse JSON: {e}")
        return []

class AutonomousModeratorCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.message_batches: dict[int, list[tuple[str, str, discord.Message]]] = defaultdict(list)
        self.last_run: dict[int, datetime] = defaultdict(lambda: datetime.now(timezone.utc))
        self.batch_runner.start()
        self.force_run: set[int] = set()

    def cog_unload(self):
        self.batch_runner.cancel()

    async def handle_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        # Early run if bot is mentioned
        if any(user.id == self.bot.user.id for user in message.mentions):
            if await mysql.get_settings(message.guild.id, "early-batch-on-mention"):
                self.force_run.add(message.guild.id)

        # Add message to cache
        normalized_message = normalize_text(message.content)
        if not normalized_message:
            return

        self.message_batches[message.guild.id].append(("Message", normalized_message, message))

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if after.author.bot or not after.guild or before.content == after.content:
            return
        self.message_batches[after.guild.id].append(("Edited Message", f"Before: {before.content}\nAfter: {after.content}", after))

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        self.message_batches[member.guild.id].append(("Member Join", f"Username: {member.name}, Display: {member.display_name}", member))

    @tasks.loop(seconds=30)
    async def batch_runner(self):
        now = datetime.now(timezone.utc)
        for gid, msgs in list(self.message_batches.items()):
            # Check required settings
            autonomous = await mysql.get_settings(gid, "autonomous-mod")
            api_key = await mysql.get_settings(gid, "api-key")
            rules = await mysql.get_settings(gid, "rules")
            if not (autonomous and api_key and rules):
                continue

            # Check interval
            interval_str = await mysql.get_settings(gid, "aimod-check-interval") or "1h"
            delta = parse_duration(interval_str) or timedelta(hours=1)

            # Build Transcript
            batch = msgs[:]
            transcript_lines = []
            for event_type, content, msg in batch:
                ts = msg.created_at.strftime("%Y-%m-%d %H:%M") if hasattr(msg, 'created_at') else datetime.now().strftime("%Y-%m-%d %H:%M")
                user_id = msg.author.id if hasattr(msg, 'author') else msg.id
                transcript_lines.append(
                    f"[{ts}] {user_id} - Message ID: {msg.id}: {content}"
                )
            transcript = "\n".join(transcript_lines)

            # Get model limit and estimate tokens
            model = await mysql.get_settings(gid, "aimod-model") or "gpt-4.1-mini"
            limit = get_model_limit(model)
            estimated_tokens = estimate_tokens(SYSTEM_MSG) + estimate_tokens(transcript)

            # Run early if transcript is too large or force run
            if gid not in self.force_run and now - self.last_run[gid] < delta and estimated_tokens < (limit * 0.9):
                continue
            self.force_run.discard(gid)
            self.last_run[gid] = now

            self.message_batches[gid].clear()
            if not batch:
                continue

            guild = self.bot.get_guild(gid)
            if not guild:
                continue

            # Prompt for AI
            user_prompt = f"Rules:\n{rules}\n\nTranscript:\n{transcript}"

            # AI call
            client = openai.AsyncOpenAI(api_key=api_key)
            try:
                completion = await client.chat.completions.create(
                    model=model,
                    messages=[{"role": "system", "content": SYSTEM_MSG}, {"role": "user", "content": user_prompt}],
                    temperature=0.0,
                    response_format={"type": "json_object"}
                )
                raw = completion.choices[0].message.content.strip()
            except Exception as e:
                print(f"[batch_runner] AI call failed for guild {gid}: {e}")
                continue

            # Parse AI response
            violations = parse_batch_response(raw)
            for item in violations:
                uid = item.get("user_id")
                actions = item.get("actions", [])
                if not (uid and actions):
                    continue

                member = guild.get_member(uid) or await guild.fetch_member(uid)
                reason = item.get("reason", "")
                rule = item.get("rule", "")
                message_ids = item.get("message_ids", [])

                # Ensure delete if message_ids are provided
                if message_ids and "delete" not in actions:
                    actions.append("delete")

                # Get message objects from message_ids
                message_ids = {int(mid) for mid in message_ids}
                messages_to_delete = [
                    msg for (_, _, msg) in batch if msg.id in message_ids
                ] if message_ids else []

                # Resolve configured actions
                configured = await mysql.get_settings(gid, AIMOD_ACTION_SETTING) or ["auto"]
                if "auto" in configured:
                    configured = actions

                # Apply actions
                await strike.perform_disciplinary_action(
                    bot=self.bot,
                    user=member,
                    action_string=configured,
                    reason=reason,
                    source="batch_ai",
                    message=messages_to_delete
                )

                # Log
                if rule and reason:
                    embed = discord.Embed(
                        title="AI-Flagged Violation",
                        description=(
                            f"User: {member.mention} ({member.name})\n"
                            f"Rule Broken: {rule}\n"
                            f"Reason: {reason}\n"
                            f"Actions: {', '.join(configured)}"
                        ),
                        colour=discord.Colour.red()
                    )
                    monitor_channel = await mysql.get_settings(gid, "monitor-channel")
                    if monitor_channel:
                        await logging.log_to_channel(embed, monitor_channel, self.bot)

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
    @app_commands.autocomplete(action=manager.autocomplete)
    async def remove_action(self, interaction: Interaction, action: str):
        msg = await manager.remove_action(interaction.guild.id, action)
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