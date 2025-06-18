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
from numpy import dot
from numpy.linalg import norm

def cosine_sim(a: list[float], b: list[float]) -> float:
    return dot(a, b) / (norm(a) * norm(b))

AIMOD_ACTION_SETTING = "aimod-detection-action"
manager = ActionListManager(AIMOD_ACTION_SETTING)

violation_cache: dict[int, deque[tuple[str, str]]] = defaultdict(lambda: deque(maxlen=3))
message_cache: dict[int, deque[tuple[str, bool, list[float]]]] = defaultdict(lambda: deque(maxlen=500))

def parse_ai_response(text: str) -> tuple[list[str], str, str, bool]:
    try:
        data = json.loads(text)
    except Exception:
        return [], "", "", False
    ok = bool(data.get("ok"))
    actions = [a.lower() for a in data.get("actions", []) if a] if not ok else []
    return actions, data.get("rule", ""), data.get("reason", ""), ok

async def embed_message(message: str, api_key: str) -> list[str]:
    try:
        client = openai.AsyncOpenAI(api_key=api_key)
        result = await client.embeddings.create(
            model="text-embedding-3-small",
            input=message
        )
        vectors = [item.embedding for item in result.data]
        return vectors
    except Exception as e:
        print(f"[embed_and_store_rules] Failed to embed message: {e}")
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

    # Embedding
    embedding_vector = await embed_message(normalized_message, api_key)
    if embedding_vector:
        vec = embedding_vector[0]
        
        # Check cache for similar "ok" messages
        for cached_text, cached_ok, cached_vec in message_cache[guild.id]:
            sim = cosine_sim(vec, cached_vec)
            if sim > 0.6 and cached_ok:
                print(f"[cache skip] Similar message in cache with sim={sim:.4f}. Skipping moderation.")
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
        model = await mysql.get_settings(guild.id, "aimod-model") or "gpt-4.1-mini"
        completion = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an AI moderator for a Discord server.\n"
                        "Only flag messages that clearly and explicitly break a rule.\n"
                        "Only take action when a message directly and unambiguously violates a listed rule.\n"
                        "Ignore sarcasm, slang, or memes unless a rule is directly broken.\n"
                        "Don't infer intent. If unclear, return ok=true.\n\n"
                        f"Server rules:\n{rules}\n{past_text}\n\n"
                        "Respond in strict JSON:\n"
                        "- ok (bool): true if no rule was broken\n"
                        "- rule (string): name of the rule broken\n"
                        "- reason (string): short explanation\n"
                        "- actions (array): any of ['delete', 'strike', 'kick', 'ban', 'timeout:<duration>', 'warn:<warning>']\n\n"
                        "Valid durations: 1s, 1m, 1h, 1d, 1w, 1mo, 1y\n"
                        "Escalate actions based on severity and past history."
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
    
    if embedding_vector:
        message_cache[guild.id].append((normalized_message, ok_flag, embedding_vector[0]))

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