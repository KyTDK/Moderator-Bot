import re
import json
import openai
import discord
from datetime import datetime, timedelta
from discord.ext import commands, tasks
from discord import app_commands, Interaction
from typing import Optional
from collections import defaultdict, deque

from modules.utils import mysql, logging
from modules.utils.time import parse_duration
from modules.moderation import strike
from modules.utils.action_manager import ActionListManager
from modules.utils.actions import VALID_ACTION_VALUES, action_choices
from modules.utils.strike import validate_action
from cogs.banned_words import normalize_text

AIMOD_ACTION_SETTING = "aimod-detection-action"
manager = ActionListManager(AIMOD_ACTION_SETTING)

violation_cache: dict[int, deque[tuple[str, str]]] = defaultdict(lambda: deque(maxlen=3))

def parse_ai_response(text: str) -> tuple[list[str], str, str, bool]:
    try:
        data = json.loads(text)
    except Exception:
        return [], "", "", False
    ok = bool(data.get("ok"))
    actions = [a.lower() for a in data.get("actions", []) if a] if not ok else []
    return actions, data.get("rule", ""), data.get("reason", ""), ok

def parse_batch_response(text: str) -> list[dict[str, str]]:
    """Parse the JSON response for batch moderation."""
    try:
        data = json.loads(text)
        if not isinstance(data, list):
            return []
        parsed = []
        for item in data:
            if not isinstance(item, dict):
                continue
            try:
                uid = int(item.get("user_id"))
            except (TypeError, ValueError):
                continue
            parsed.append({
                "user_id": uid,
                "rule": str(item.get("rule", "")),
                "reason": str(item.get("reason", "")),
                "action": str(item.get("action", "")),
            })
        return parsed
    except Exception:
        return []

async def moderate_event(
    bot,
    guild: discord.Guild,
    user: discord.User,
    event_type: str,
    content: str,
    message_obj: Optional[discord.Message] = None
):
    # Check required settings
    autonomous = await mysql.get_settings(guild.id, "autonomous-mod")
    api_key = await mysql.get_settings(guild.id, "api-key")
    rules = await mysql.get_settings(guild.id, "rules")
    if not (autonomous and api_key and rules):
        return

    normalized_message = normalize_text(content)
    if not normalized_message:
        return

    # Context block
    contextual_enabled = await mysql.get_settings(guild.id, "contextual-ai")
    context_lines = []

    if contextual_enabled and message_obj and message_obj.channel:
        try:
            ref = message_obj.reference
            replied = getattr(ref, "resolved", None) if ref else None
            if replied is None and ref and ref.message_id:
                try:
                    replied = await message_obj.channel.fetch_message(ref.message_id)
                except Exception:
                    replied = None

            if replied and not replied.author.bot and replied.content:
                # Only include the replied-to message
                context_lines = [
                    f"[{replied.created_at.strftime('%H:%M')}] @{replied.author.display_name} (replied-to): {normalize_text(replied.content)}"
                ]
            else:
                # Only include previous messages if no reply target
                async for msg in message_obj.channel.history(limit=10, before=message_obj, oldest_first=False):
                    if msg.author.bot or not msg.content.strip():
                        continue
                    context_lines.append(
                        f"[{msg.created_at.strftime('%H:%M')}] @{msg.author.display_name}: {normalize_text(msg.content)}"
                    )
                    if len(context_lines) >= 5:
                        break
                context_lines.reverse()
        except Exception as e:
            print(f"[moderate_event] Context build failed: {e}")

    # Prepare violation history
    history = violation_cache[user.id]
    past_text = (
        "\n\nPrevious Violations:\n" + "\n".join(
            f"- {i}. Rule: {r} | Reason: {t}" for i, (r, t) in enumerate(history, 1)
        )
        if history else "This user has no prior violations."
    )

    # Build prompt dynamically
    if context_lines:
        context_text = "\n".join(context_lines)
        user_prompt = (
            f"Context:\n{context_text}\n\n"
            f"User message:\n\"{normalized_message}\"\n\n"
            f"Event type: {event_type}\n"
            "Only evaluate the user's message. Use the context only if clearly needed."
        )
    else:
        user_prompt = (
            f"User message:\n\"{normalized_message}\"\n\n"
            f"Event type: {event_type}\n"
            "Evaluate this message alone for rule violations."
        )

    # OpenAI API call
    client = openai.AsyncOpenAI(api_key=api_key)
    try:
        model = await mysql.get_settings(guild.id, "aimod-model") or "gpt-4.1-nano"
        completion = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an AI that checks whether a message violates specific server rules.\n"
                        "Only flag messages that clearly and unambiguously break a rule below. If unsure, return ok=true.\n"
                        "Ignore sarcasm, slang, memes, or profanity unless it directly breaks a rule. Do not guess intent.\n"
                        "Sexual or offensive language is allowed unless a rule explicitly prohibits it.\n"
                        "Do not enforce general platform policies — only the listed rules matter.\n\n"
                        f"Server rules:\n{rules}\n{past_text}\n\n"
                        "Respond in strict JSON:\n"
                        "- ok: true | false\n"
                        "- rule: \"<broken-rule-name>\" (empty if ok)\n"
                        "- reason: \"<brief explanation>\"\n"
                        "- actions: [ 'delete' | 'strike' | 'kick' | 'ban' | 'timeout:<dur>' | 'warn:<text>' ]\n\n"
                        "Valid durations: 1s 1m 1h 1d 1w 1mo 1y.\n"
                        "Escalate actions based on repeat offenses using the history above."
                    )
                },
                {
                    "role": "user",
                    "content": user_prompt
                }
            ],
            temperature=0.0,
            response_format={"type": "json_object"}
        )
    except Exception as e:
        print(f"[moderate_event] AI call failed: {e}")
        return

    raw = completion.choices[0].message.content.strip()
    actions_from_ai, rule_broken, reason_text, ok_flag = parse_ai_response(raw)
    if ok_flag or not actions_from_ai:
        return

    # Record violation
    if rule_broken and reason_text:
        violation_cache[user.id].appendleft((rule_broken, reason_text))

    # Log and take action
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

    configured = await mysql.get_settings(guild.id, AIMOD_ACTION_SETTING) or ["auto"]
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
        self.message_batches: dict[int, list[tuple[str, str, discord.Message]]] = defaultdict(list)
        self.last_run: dict[int, datetime] = defaultdict(lambda: datetime.utcnow())
        self.batch_runner.start()

    def cog_unload(self):
        self.batch_runner.cancel()

    async def handle_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        self.message_batches[message.guild.id].append(("Message", message.content, message))

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if after.author.bot or not after.guild or before.content == after.content:
            return
        self.message_batches[after.guild.id].append(
            (
                "Edited Message",
                f"Before: {before.content}\nAfter: {after.content}",
                after,
            )
        )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        await moderate_event(self.bot, member.guild, member, "Member Join",
                             f"Username: {member.name}, Display: {member.display_name}")

    @tasks.loop(minutes=1)
    async def batch_runner(self):
        now = datetime.utcnow()
        for gid, msgs in list(self.message_batches.items()):
            interval_str = await mysql.get_settings(gid, "aimod-check-interval") or "4h"
            delta = parse_duration(str(interval_str)) or timedelta(hours=4)
            last = self.last_run.get(gid)
            if last and now - last < delta:
                continue

            self.last_run[gid] = now
            batch = msgs[:]
            self.message_batches[gid].clear()

            if not batch:
                continue

            guild = self.bot.get_guild(gid)
            if not guild:
                continue

            api_key = await mysql.get_settings(gid, "api-key")
            rules = await mysql.get_settings(gid, "rules")
            if not (api_key and rules):
                continue

            transcript_lines = []
            last_message_per_user = {}
            for event_type, content, msg in batch:
                ts = msg.created_at.strftime("%Y-%m-%d %H:%M")
                transcript_lines.append(
                    f"[{ts}] {msg.author.display_name} ({msg.author.id}): {content}"
                )
                last_message_per_user[msg.author.id] = msg

            transcript = "\n".join(transcript_lines)

            history_blocks = []
            for uid in last_message_per_user:
                history = violation_cache[uid]
                if history:
                    joined = "; ".join(f"{r} - {t}" for r, t in history)
                    history_blocks.append(f"{uid}: {joined}")
            history_text = "\n".join(history_blocks) if history_blocks else "None"

            system_msg = (
                "You are a Discord moderation assistant. "
                "Review the transcript and identify messages that break the server rules. "
                "Use the provided violation history to escalate punishments. "
                "Return a JSON array of objects with fields: user_id, rule, reason, action. "
                "Valid actions are: delete, strike, kick, ban, timeout:<duration>, warn:<text>."
            )

            user_prompt = (
                f"Rules:\n{rules}\n\nViolation history:\n{history_text}\n\nTranscript:\n{transcript}"
            )

            client = openai.AsyncOpenAI(api_key=api_key)
            try:
                model = await mysql.get_settings(gid, "aimod-model") or "gpt-4.1-nano"
                completion = await client.chat.completions.create(
                    model=model,
                    messages=[{"role": "system", "content": system_msg}, {"role": "user", "content": user_prompt}],
                    temperature=0.0,
                    response_format={"type": "json_object"}
                )
                raw = completion.choices[0].message.content.strip()
            except Exception as e:
                print(f"[batch_runner] AI call failed for guild {gid}: {e}")
                continue

            violations = parse_batch_response(raw)
            for item in violations:
                uid = item.get("user_id")
                action = item.get("action", "").lower()
                if not (uid and action):
                    continue
                member = guild.get_member(uid)
                if member is None:
                    try:
                        member = await guild.fetch_member(uid)
                    except Exception:
                        continue

                reason = item.get("reason", "")
                rule = item.get("rule", "")
                msg_obj = last_message_per_user.get(uid)
                await strike.perform_disciplinary_action(
                    bot=self.bot,
                    user=member,
                    action_string=action,
                    reason=reason,
                    source="batch_ai",
                    message=msg_obj,
                )
                if rule and reason:
                    violation_cache[uid].appendleft((rule, reason))

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