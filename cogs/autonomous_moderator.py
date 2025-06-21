import json
import openai
import discord
from datetime import datetime, timedelta, timezone
from discord.ext import commands, tasks
from discord import app_commands, Interaction
from collections import defaultdict, deque

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
violation_cache: dict[int, deque[tuple[str, str]]] = defaultdict(lambda: deque(maxlen=3)) # user_id -> deque of (rule: str, action: str)

SYSTEM_MSG = (
    "You are an AI moderator.\n"
    "The next user message will begin with 'Rules:' — those are the ONLY rules you are allowed to enforce.\n\n"
    "Core constraints:\n"
    "- No personal judgment or outside policies. Only enforce what is explicitly stated under 'Rules:'.\n"
    "- No overreach. Ignore sarcasm, vague innuendo, or mere references. Only act on clear, explicit rule violations.\n"
    "- Do not punish reporters. Never flag messages that quote, reference, or accuse others — only punish the speaker.\n"
    "- Do not use prior violations unless the current message directly continues the same harmful pattern.\n"
    "- If you are unsure, default to ok=true.\n\n"

    "Respond with a JSON object containing a `results` field.\n"
    "`results` must be an array of violations. Each violation must include:\n"
    "- user_id (string)\n"
    "- rule (string)\n"
    "- reason (string)\n"
    "- actions (array of punishments)\n"
    "- message_ids (optional array of message IDs to delete)\n\n"

    "If any message_ids are listed, always include 'delete' in the actions array.\n"
    "Valid actions: delete, strike, kick, ban, timeout:<duration>, warn:<text>.\n\n"
    "Use timeout:<duration> with a clear time unit. Durations must include a unit like s, m, h, d, w, or mo (e.g., 10m for 10 minutes)."

    "Punishment meanings:\n"
    "- warn:<text>: Warn the user.\n"
    "- delete: Always include this for rule-breaking messages to remove them from chat.\n"
    "- timeout:<duration>: Temporarily mute the user.\n"
    "- kick: Remove user from server (temporary).\n"
    "- strike: Permanent record which comes with its own punishment.\n"
    "- ban: Permanent removal from the server.\n"
)

BASE_SYSTEM_TOKENS = ceil(len(SYSTEM_MSG) / 4)

def estimate_tokens(text: str) -> int:
    return ceil(len(text) / 4)

MODEL_LIMITS = {
    "gpt-4.1": 1000000,
    "gpt-4.1-nano": 1000000,
    "gpt-4.1-mini": 1000000,
    "gpt-4o": 128000,
    "gpt-4o-mini": 128000
}

def get_model_limit(model_name: str) -> int:
    return next((limit for key, limit in MODEL_LIMITS.items() if key in model_name), 16000)

def parse_batch_response(text: str) -> list[dict[str, object]]:
    try:
        data = json.loads(text).get("results", [])
    except Exception as e:
        print(f"[parse_batch_response] Failed to parse JSON: {e}")
        return []

    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []

    results = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            uid = int(item.get("user_id"))
        except (TypeError, ValueError):
            continue

        actions = item.get("actions") or item.get("action") or []
        if isinstance(actions, str):
            actions = [actions]

        results.append(
            {
                "user_id": uid,
                "rule": item.get("rule", ""),
                "reason": item.get("reason", ""),
                "actions": [a.lower() for a in actions if isinstance(a, str)],
                "message_ids": item.get("message_ids", []),
            }
        )
    return results

class AutonomousModeratorCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.message_batches: dict[int, list[tuple[str, str, discord.Message]]] = defaultdict(list)
        self.last_run: dict[int, datetime] = defaultdict(lambda: datetime.now(timezone.utc))
        self.batch_runner.start()
        self.mention_triggers: dict[int, discord.Message] = {}

    def cog_unload(self):
        self.batch_runner.cancel()

    def _format_event(self, msg: discord.Message, content: str) -> str:
        ts = getattr(msg, "created_at", datetime.now()).strftime("%Y-%m-%d %H:%M")
        user_id = getattr(getattr(msg, "author", None), "id", msg.id)
        return f"[{ts}] {user_id} - Message ID: {msg.id}: {content}"

    def _build_transcript(self, batch: list[tuple[str, str, discord.Message]], max_tokens: int, current_total_tokens: int):
        lines = [self._format_event(msg, text) for _, text, msg in batch]
        tokens = [estimate_tokens(line) for line in lines]
        total = current_total_tokens + sum(tokens)
        while batch and total > max_tokens:
            total -= tokens.pop(0)
            batch.pop(0)
            lines.pop(0)
        transcript = "\n".join(lines)
        return transcript, total, batch

    async def handle_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        settings = await mysql.get_settings(message.guild.id, [
            "autonomous-mod",
            "aimod-mode"
        ])

        if not settings.get("autonomous-mod"):
            return

        # Early run if bot is mentioned
        if any(user.id == self.bot.user.id for user in message.mentions):
            # Report mode, add trigger and acknowledge the mention
            if settings.get("aimod-mode") == "report":
                await message.add_reaction("👀")
                self.mention_triggers[message.guild.id] = message

        # Interval, cache all messages
        if settings.get("aimod-mode") == "interval":
            # Add message to cache
            normalized_message = normalize_text(message.content)
            if not normalized_message:
                return
            guild_batch = self.message_batches[message.guild.id]
            guild_batch.append(("Message", normalized_message, message))

            # Cap max stored messages per guild
            if len(guild_batch) > 1000:
                guild_batch.pop(0)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if after.author.bot or not after.guild or before.content == after.content:
            return
        normalized_before = normalize_text(before.content)
        normalized_after = normalize_text(after.content)
        if normalized_before and normalized_after:
            self.message_batches[after.guild.id].append(("Edited Message", f"Before: {normalized_before}\nAfter: {normalized_after}", after))

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        self.message_batches[member.guild.id].append(("Member Join", f"Username: {member.name}, Display: {member.display_name}", member))

    @tasks.loop(seconds=30)
    async def batch_runner(self):
        now = datetime.now(timezone.utc)
        guild_ids = set(self.message_batches.keys()) | set(self.mention_triggers.keys())
        for gid in guild_ids:
            msgs = self.message_batches.get(gid, [])
            # Check required settings
            settings = await mysql.get_settings(gid, [
                "autonomous-mod",
                "api-key",
                "rules",
                "aimod-mode",
                "aimod-check-interval",
                "aimod-model",
                "monitor-channel",
                AIMOD_ACTION_SETTING
            ])

            # Check essential settings
            autonomous = settings.get("autonomous-mod")
            api_key = settings.get("api-key")
            rules = settings.get("rules")
            if not (autonomous and api_key and rules):
                continue

            # Skip if report mode but no mention trigger
            aimod_mode = settings.get("aimod-mode")
            if aimod_mode == "report" and gid not in self.mention_triggers:
                continue  

            # Check interval
            interval_str = settings.get("aimod-check-interval") or "1h"
            delta = parse_duration(interval_str) or timedelta(hours=1)

            # Skip if interval mode but not time to check
            if aimod_mode == "interval" and now - self.last_run[gid] < delta:
                continue
            
            # Get batch, trigger message, prepare rules
            batch = msgs[:]
            trigger_msg = self.mention_triggers.pop(gid, None)
            rules = f"Rules:\n{rules}\n\n"

            # If report mode, fetch messages from that channel
            if aimod_mode == "report":
                if not trigger_msg:
                    continue
                try:
                    print(f"[AutonomousModerator] Fetching channel history for guild id {gid}")
                    fetched = [msg async for msg in trigger_msg.channel.history(limit=50)]
                    fetched.sort(key=lambda m: m.created_at)  # Add this
                    for msg in fetched:
                        normalized = normalize_text(msg.content)
                        if normalized:
                            batch.append(("Fetched Message", normalized, msg))
                except discord.HTTPException as e:
                    print(f"[AI] Failed to fetch history in guild {gid}: {e}")

            # Build violation history
            user_ids = {msg.author.id for _, _, msg in batch if hasattr(msg, 'author')}
            violation_blocks = []
            for uid in user_ids:
                history = violation_cache[uid]
                if history:
                    lines = [f"{i+1}. {reason} — previously punished with {action}" for i, (reason, action) in enumerate(history)]
                    joined = "\n".join(lines)
                    violation_blocks.append(f"User {uid} has {len(history)} recent violation(s):\n{joined}")
            violation_history = "\n".join(violation_blocks)
            if not violation_history:
                violation_history = "No recent violations on record."
            violation_history = f"Violation history:\n{violation_history}\n\n"

            # Build transcript with truncation if needed
            model = settings.get("aimod-model") or "gpt-4.1-mini"
            limit = get_model_limit(model)
            max_tokens = int(limit * 0.9)
            current_total_tokens=BASE_SYSTEM_TOKENS+estimate_tokens(violation_history)+estimate_tokens(rules)
            transcript, estimated_tokens, batch = self._build_transcript(batch, 
                                                                         max_tokens, 
                                                                         current_total_tokens=current_total_tokens)

            # Skip if too many tokens
            if estimated_tokens >= max_tokens:
                continue
            self.last_run[gid] = now

            self.message_batches[gid].clear()
            if not batch:
                continue

            guild = self.bot.get_guild(gid)
            if not guild:
                continue

            # Prompt for AI
            user_prompt = f"{rules}{violation_history}Transcript:\n{transcript}"

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
                print(raw)
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
                message_ids = {int(mid) for mid in item.get("message_ids", [])}

                # Ensure delete if message_ids are provided
                if message_ids and "delete" not in actions:
                    actions.append("delete")

                messages_to_delete = [msg for (_, _, msg) in batch if msg.id in message_ids] if message_ids else []

                # Resolve configured actions
                configured = settings.get(AIMOD_ACTION_SETTING) or ["auto"]
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
                    violation_cache[uid].append((rule, ", ".join(configured)))
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
                    monitor_channel = settings.get("monitor-channel")
                    if monitor_channel:
                        await logging.log_to_channel(embed, monitor_channel, self.bot)
            # Send feedback if this batch was triggered by mention
            if trigger_msg:
                try:
                    if violations:
                        await trigger_msg.reply("Thanks for the report. Action was taken.")
                    else:
                        await trigger_msg.reply("Thanks for the report. No violations were found.")
                except discord.HTTPException:
                    pass

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
