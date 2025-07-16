import json
import openai
import discord
from datetime import datetime, timedelta, timezone
from discord.ext import commands, tasks
from collections import defaultdict, deque

from modules.utils import mod_logging, mysql
from modules.utils.discord_utils import safe_get_member
from modules.utils.time import parse_duration
from modules.moderation import strike

from math import ceil
import re

AIMOD_ACTION_SETTING = "aimod-detection-action"

violation_cache: dict[int, deque[tuple[str, str]]] = defaultdict(lambda: deque(maxlen=10)) # user_id -> deque of (rule: str, action: str)

SYSTEM_MSG = (
    "You are an AI moderator.\n"
    "The next user message will begin with 'Rules:' — those are the ONLY rules you are allowed to enforce.\n\n"
    "Core constraints:\n"
    "- No personal judgment or outside policies. Only enforce what is explicitly stated under 'Rules:'.\n"
    "- No overreach. Ignore sarcasm, vague innuendo, or mere references. Only act on clear, explicit rule violations.\n"
    "- Do not punish reporters. Never flag messages that quote, reference, or accuse others — only punish the speaker.\n"
    "- Only use prior violations to identify persistent rule-breaking. They may support your reasoning, but do not justify punishment on their own. The current message must clearly break a rule.\n"
    "- If you are unsure, default to ok=true.\n\n"

    "Respond with a JSON object containing a `results` field.\n"
    "`results` must be an array of violations. Each violation must include:\n"
    "- user_id (string)\n"
    "- rule (string)\n"
    "- reason (string)\n"
    "- actions (array of punishments)\n"
    "- message_ids (array of message IDs that violated the rule). You must include the ID(s) of the specific message(s) that broke the rule.\n\n"

    "If any message_ids are listed, always include 'delete' in the actions array.\n"
    "Valid actions: delete, strike, kick, ban, timeout:<duration>, warn:<text>.\n\n"
    "Use timeout:<duration> with a clear time unit. Durations must include a unit like s, m, h, d, w, or mo (e.g., 10m for 10 minutes)."

    "Punishment meanings:\n"
    "- warn:<text>: Warn the user.\n"
    "- delete: Always include this for rule-breaking messages to remove them from chat.\n"
    "- timeout:<duration>: Temporarily mute the user.\n"
    "- kick: Remove user from server (temporary).\n"
    "- strike: Permanent record which comes with its own punishment.\n"
    "- ban: Permanent removal from the server."
)

BASE_SYSTEM_TOKENS = ceil(len(SYSTEM_MSG) / 4)
NEW_MEMBER_THRESHOLD = timedelta(hours=48)

IMAGE_EXT  = re.compile(r"\.(?:png|jpe?g|webp|bmp|tiff?)$", re.I)
GIF_EXT    = re.compile(r"\.(?:gif|apng)$", re.I)
TENOR_RE   = re.compile(r"(?:tenor\.com|giphy\.com)", re.I)
VIDEO_EXT   = re.compile(r"\.(?:mp4|m4v|webm|mov|avi|mkv|gifv)$", re.I)

def collapse_media(url: str) -> str:
    """Return a short placeholder if the URL points to an image/GIF."""
    if TENOR_RE.search(url):
        return "[gif]"
    if GIF_EXT.search(url):
        return "[gif]"
    if IMAGE_EXT.search(url):
        return "[image]"
    if VIDEO_EXT.search(url):
        return "[video]"
    return url

def estimate_tokens(text: str) -> int:
    return ceil(len(text) / 4)

MODEL_LIMITS = {
    "gpt-4.1": 1000000,
    "gpt-4.1-nano": 1000000,
    "gpt-4.1-mini": 1000000,
    "gpt-4o": 128000,
    "gpt-4o-mini": 128000
}

async def get_active_mode(guild_id: int) -> str:
    mode = await mysql.get_settings(guild_id, "aimod-mode")
    if mode == "adaptive":
        return await mysql.get_settings(guild_id, "aimod-active-mode") or "report"
    return mode

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


    async def _format_event(
        self,
        msg: discord.Message,
        content: str,
        tag: str,
        delta: timedelta | None
    ) -> str:
        author = await safe_get_member(msg.guild, msg.author.id)
        if not author:
            return None
        tokens  = [collapse_media(w) if w.startswith("http") else w
                for w in content.split()]
        content = " ".join(tokens)

        if delta is None:
            time_since = "First message in batch."
        else:
            mins, secs = divmod(int(delta.total_seconds()), 60)
            time_since = f"{mins} min {secs}s after previous." if mins \
                        else f"{secs}s after previous."

        joined_at = getattr(author, "joined_at", None)
        new_member = ""
        if joined_at:
            age = msg.created_at - joined_at
            if age < NEW_MEMBER_THRESHOLD:
                m_mins, m_secs = divmod(int(age.total_seconds()), 60)
                m_hours, m_mins = divmod(m_mins, 60)
                parts = [f"{m_hours}h" if m_hours else "",
                        f"{m_mins}m" if m_mins else "",
                        f"{m_secs}s" if not m_hours and not m_mins else ""]
                pretty_age = " ".join(p for p in parts if p)
                new_member = f"\nNOTE: joined server {pretty_age} ago."

        return (
            f"[{time_since}]{new_member}\n"
            f"{tag.upper()}\n"
            f"user = {author.display_name} (id = {author.id})\n"
            f"message_id = {msg.id}\n"
            f"content = {content}\n"
            "---"
        )

    async def _build_transcript(
        self,
        batch: list[tuple[str, str, discord.Message]],
        max_tokens: int,
        current_total_tokens: int
    ):
        lines   : list[str] = []
        tokens  : list[int] = []
        trimmed_batch = batch[:]
        prev_time: datetime | None = None

        for tag, text, msg in trimmed_batch:
            timestamp = msg.created_at.replace(tzinfo=timezone.utc)
            delta = timestamp - prev_time if prev_time else None
            prev_time = timestamp

            line = await self._format_event(msg, text, tag, delta)
            if line:
                tok  = estimate_tokens(line)

                lines.append(line)
                tokens.append(tok)

        total_tokens = current_total_tokens + sum(tokens)

        while trimmed_batch and total_tokens > max_tokens:
            total_tokens -= tokens.pop(0)
            trimmed_batch.pop(0)
            lines.pop(0)

        transcript = "\n".join(lines)
        return transcript, total_tokens, trimmed_batch

    async def handle_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        
        if not await mysql.get_settings(message.guild.id, "autonomous-mod"):
            return

        active_mode = await get_active_mode(message.guild.id)

        # Early run if bot is mentioned
        if f"<@{self.bot.user.id}>" in message.content:
            # Report mode, add trigger and acknowledge the mention
            if active_mode == "report":                
                await message.add_reaction("👀")
                self.mention_triggers[message.guild.id] = message

        # Interval, cache all messages
        if active_mode == "interval":
            # Add message to cache
            guild_batch = self.message_batches[message.guild.id]
            guild_batch.append(("Message", message.content, message))

            # Cap max stored messages per guild
            if len(guild_batch) > 1000:
                guild_batch.pop(0)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if after.author.bot or not after.guild or before.content == after.content:
            return
        self.message_batches[after.guild.id].append((
            "Edited Message", f"(edited)\n> Before: {before.content}\n> After:  {after.content}", after
        ))


    @tasks.loop(seconds=5)
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
            active_mode = await get_active_mode(gid)
            if active_mode == "report" and gid not in self.mention_triggers:
                continue  

            # Check interval
            interval_str = settings.get("aimod-check-interval") or "1h"
            delta = parse_duration(interval_str) or timedelta(hours=1)

            # Skip if interval mode but not time to check
            if active_mode == "interval" and now - self.last_run[gid] < delta:
                continue
            
            # Get batch, trigger message, prepare rules
            batch = msgs[:]
            trigger_msg = self.mention_triggers.pop(gid, None)
            rules = f"Rules:\n{rules}\n\n"

            # If report mode, fetch messages from that channel
            if active_mode == "report":
                if not trigger_msg:
                    continue
                try:
                    fetched = [msg async for msg in trigger_msg.channel.history(limit=50)]
                    fetched.sort(key=lambda m: m.created_at)
                    for msg in fetched:
                        content = msg.content
                        if content:
                            if msg.reference:
                                content = f"(response to message_id={msg.reference.message_id}) {content}"
                            batch.append(("Message", content, msg))
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
            transcript, estimated_tokens, batch = await self._build_transcript(batch, 
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
                        await mod_logging.log_to_channel(embed, monitor_channel, self.bot)
            # Send feedback if this batch was triggered by mention
            if trigger_msg:
                try:
                    if violations:
                        await trigger_msg.reply("Thanks for the report. Action was taken.")
                    else:
                        await trigger_msg.reply("Thanks for the report. No violations were found.")
                except discord.HTTPException:
                    pass

async def setup_autonomous(bot: commands.Bot):
    await bot.add_cog(AutonomousModeratorCog(bot))