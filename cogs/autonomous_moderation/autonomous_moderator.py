import openai
import discord
from datetime import datetime, timedelta, timezone
from discord.ext import commands, tasks
from collections import defaultdict, deque

from modules.cache import CachedMessage
from modules.utils import mod_logging, mysql
from modules.utils.discord_utils import safe_get_member
from modules.utils.time import parse_duration
from cogs.autonomous_moderation.models import ModerationReport
from cogs.autonomous_moderation import helpers as am_helpers
from cogs.autonomous_moderation.prompt import (
    SYSTEM_PROMPT,
    BASE_SYSTEM_TOKENS,
    get_model_limit,
    NEW_MEMBER_THRESHOLD_HOURS,
)

from dotenv import load_dotenv
import os

load_dotenv()
AUTOMOD_OPENAI_KEY = os.getenv('AUTOMOD_OPENAI_KEY')
AIMOD_MODEL = os.getenv('AIMOD_MODEL', 'gpt-5-nano')
# Pricing and budget: $0.45 per 1M tokens, $2 budget per cycle
PRICE_PER_MTOK = 0.45
PRICE_PER_TOKEN = PRICE_PER_MTOK / 1_000_000
BUDGET_USD = 2.0

PRICES_PER_MTOK = {
    'gpt-5-nano': 0.45,
    'gpt-5-mini': 2.25,
}

def get_price_per_mtok(model_name: str) -> float:
    return next((v for k, v in PRICES_PER_MTOK.items() if k in model_name), PRICE_PER_MTOK)


AIMOD_ACTION_SETTING = "aimod-detection-action"

violation_cache: dict[int, deque[tuple[str, str]]] = defaultdict(lambda: deque(maxlen=10)) # user_id -> deque of (rule: str, action: str)

# Derived constant for transcript annotations
NEW_MEMBER_THRESHOLD = timedelta(hours=NEW_MEMBER_THRESHOLD_HOURS)

async def get_active_mode(guild_id: int) -> str:
    mode = await mysql.get_settings(guild_id, "aimod-mode")
    if mode == "adaptive":
        return await mysql.get_settings(guild_id, "aimod-active-mode") or "report"
    return mode

class AutonomousModeratorCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.message_batches: dict[int, list[tuple[str, str, discord.Message]]] = defaultdict(list)
        self.last_run: dict[int, datetime] = defaultdict(lambda: datetime.now(timezone.utc))
        self.batch_runner.start()
        self.mention_triggers: dict[int, discord.Message] = {}

    def cog_unload(self):
        self.batch_runner.cancel()

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

    async def handle_message_edit(self, cached_before: CachedMessage, after: discord.Message):
        cached_before_content = cached_before.content
        if cached_before_content is not None:
            self.message_batches[after.guild.id].append((
                "Edited Message", f"(edited)\n> Before: {cached_before_content}\n> After:  {after.content}", after
            ))

    @tasks.loop(seconds=5)
    async def batch_runner(self):
        now = datetime.now(timezone.utc)
        guild_ids = set(self.message_batches.keys()) | set(self.mention_triggers.keys())
        for gid in guild_ids:
            msgs = self.message_batches.get(gid, [])

            settings = await mysql.get_settings(
                gid,
                [
                    "autonomous-mod",
                    "rules",
                    "aimod-check-interval",
                    "monitor-channel",
                    "aimod-channel",
                    "aimod-debug",
                    AIMOD_ACTION_SETTING,
                    "aimod-high-accuracy",
                ],
            )

            autonomous = settings.get("autonomous-mod")
            api_key = AUTOMOD_OPENAI_KEY
            rules = settings.get("rules")
            if not (autonomous and api_key and rules):
                continue

            active_mode = await get_active_mode(gid)
            if active_mode == "report" and gid not in self.mention_triggers:
                continue

            interval_str = settings.get("aimod-check-interval") or "1h"
            delta = parse_duration(interval_str) or timedelta(hours=1)
            if active_mode == "interval" and now - self.last_run[gid] < delta:
                continue

            batch = msgs[:]
            trigger_msg = self.mention_triggers.pop(gid, None)
            rules = f"Rules:\n{rules}\n\n"

            if active_mode == "report":
                if not trigger_msg:
                    continue
                try:
                    batch.extend(await am_helpers.prepare_report_batch(trigger_msg))
                except discord.HTTPException as e:
                    print(f"[AI] Failed to fetch history in guild {gid}: {e}")

            # Build violation history (modular helper)
            violation_history = am_helpers.build_violation_history(batch, violation_cache)

            # Build transcript with truncation if needed
            high_accuracy = settings.get("aimod-high-accuracy") or False
            model_for_guild = 'gpt-5-mini' if high_accuracy else AIMOD_MODEL
            limit = get_model_limit(model_for_guild)
            max_tokens = int(limit * 0.9)
            current_total_tokens = (
                BASE_SYSTEM_TOKENS
                + am_helpers.estimate_tokens(violation_history)
                + am_helpers.estimate_tokens(rules)
            )
            transcript, estimated_tokens, batch = await am_helpers.build_transcript(
                batch,
                max_tokens,
                current_total_tokens,
                NEW_MEMBER_THRESHOLD,
            )

            # Skip if too many tokens
            if estimated_tokens >= max_tokens:
                continue
            if not batch:
                continue

            # Budget check before calling AI
            usage = await mysql.get_aimod_usage(gid)
            price_per_mtok = get_price_per_mtok(model_for_guild)
            price_per_token = price_per_mtok / 1_000_000
            request_cost = round(estimated_tokens * price_per_token, 6)
            if (usage.get("cost_usd", 0.0) + request_cost) > usage.get("limit_usd", BUDGET_USD):
                # Notify reporter if applicable
                if trigger_msg:
                    try:
                        cycle_end = usage.get("cycle_end")
                        reset_str = cycle_end.strftime('%Y-%m-%d %H:%M UTC') if cycle_end else "the next cycle"
                        await trigger_msg.reply(
                            f"AI moderation budget reached for this billing cycle. Resets at {reset_str}.",
                            mention_author=False,
                        )
                    except discord.HTTPException:
                        pass
                # Keep batches so we can try again next cycle
                continue

            self.last_run[gid] = now
            # Clear batch only when proceeding with a scan
            self.message_batches[gid].clear()

            guild = self.bot.get_guild(gid)
            if not guild:
                continue

            # Prompt for AI
            user_prompt = f"{rules}{violation_history}Transcript:\n{transcript}"

            # AI call
            client = openai.AsyncOpenAI(api_key=api_key)
            try:
                kwargs = {
                    "model": model_for_guild,
                    "instructions": SYSTEM_PROMPT,
                    "input": user_prompt,
                    "text_format": ModerationReport, 
                }
                if model_for_guild.startswith("gpt-5"):
                    kwargs["reasoning"] = {"effort": "minimal"}

                completion = await client.responses.parse(**kwargs)
                report: ModerationReport | None = completion.output_parsed
            except Exception as e:
                print(f"[batch_runner] AI call failed for guild {gid}: {e}")
                continue

            # Record usage after a successful API call
            try:
                await mysql.add_aimod_usage(gid, estimated_tokens, request_cost)
            except Exception as e:
                print(f"[aimod_usage] Failed to record usage for guild {gid}: {e}")

            aimod_debug = settings.get("aimod-debug") or False
            ai_channel_id = settings.get("aimod-channel")
            monitor_channel_id = settings.get("monitor-channel")
            debug_log_channel = ai_channel_id or monitor_channel_id

            if not report or not report.violations:
                if trigger_msg:
                    try:
                        await trigger_msg.reply("Thanks for the report. No violations were found.")
                    except discord.HTTPException:
                        pass
                # Always log to AI violations channel when debug is enabled
                if aimod_debug and debug_log_channel:
                    mode_str = await get_active_mode(gid)
                    embed = am_helpers.build_no_violations_embed(len(batch), mode_str)
                    await mod_logging.log_to_channel(embed, debug_log_channel, self.bot)
                continue

            # Aggregate: at most one violation per user per scan (modular helper)
            aggregated, fanout_authors = am_helpers.aggregate_violations(
                report.violations, batch
            )

            # Apply once per author
            for author_id, data in aggregated.items():
                member = await safe_get_member(guild, author_id)
                if not member:
                    continue

                all_actions = list(data["actions"]) if data.get("actions") else []
                configured = am_helpers.resolve_configured_actions(
                    settings, all_actions, AIMOD_ACTION_SETTING
                )

                reasons = data.get("reasons") or []
                if not reasons:
                    out_reason = "Violation detected"
                elif len(reasons) == 1:
                    out_reason = reasons[0]
                else:
                    out_reason = "Multiple violations: " + "; ".join(reasons)

                rules = list(data.get("rules") or [])
                if not rules:
                    out_rule = "Rule violation"
                elif len(rules) == 1:
                    out_rule = rules[0]
                else:
                    out_rule = "Multiple rules: " + ", ".join(rules)

                await am_helpers.apply_actions_and_log(
                    bot=self.bot,
                    member=member,
                    configured_actions=configured,
                    reason=out_reason,
                    rule=out_rule,
                    messages=data.get("messages") or [],
                    aimod_debug=aimod_debug,
                    ai_channel_id=ai_channel_id,
                    monitor_channel_id=monitor_channel_id,
                    ai_actions=all_actions,
                    fanout=(author_id in fanout_authors),
                    violation_cache=violation_cache,
                )

            if trigger_msg:
                try:
                    await trigger_msg.reply("Thanks for the report. Action was taken.")
                except discord.HTTPException:
                    pass

async def setup_autonomous(bot: commands.Bot):
    await bot.add_cog(AutonomousModeratorCog(bot))
