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
    NEW_MEMBER_THRESHOLD_HOURS,
)
from modules.ai.mod_utils import get_model_limit, pick_model
from modules.ai.pipeline import run_moderation_pipeline

from dotenv import load_dotenv
import os

from modules.core.moderator_bot import ModeratorBot

load_dotenv()
PRIMARY_OPENAI_KEY = os.getenv('PRIMARY_OPENAI_KEY')
AIMOD_MODEL = os.getenv('AIMOD_MODEL', 'gpt-5-nano')


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
    def __init__(self, bot: ModeratorBot):
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
            api_key = PRIMARY_OPENAI_KEY
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
            model_for_guild = pick_model(high_accuracy, AIMOD_MODEL)
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

            # Run unified moderation pipeline
            try:
                report, _total_tokens, request_cost, usage, status = await run_moderation_pipeline(
                    guild_id=gid,
                    api_key=api_key,
                    system_prompt=SYSTEM_PROMPT,
                    rules=rules,
                    violation_history_blob=violation_history,
                    transcript=transcript,
                    base_system_tokens=BASE_SYSTEM_TOKENS,
                    default_model=model_for_guild,
                    high_accuracy=False,
                    text_format=ModerationReport,
                    estimate_tokens_fn=am_helpers.estimate_tokens,
                    precomputed_total_tokens=estimated_tokens,
                )
            except Exception as e:
                print(f"[batch_runner] AI call failed for guild {gid}: {e}")
                continue

            self.last_run[gid] = now
            # Clear batch only when proceeding with a scan
            self.message_batches[gid].clear()

            guild = self.bot.get_guild(gid)
            if not guild:
                continue

            guild_locale = self.bot.resolve_locale(guild)

            # Budget notification if applicable
            if report is None and trigger_msg and status == "budget":
                try:
                    cycle_end = usage.get("cycle_end")
                    reset_label = (
                        self.bot.translate(
                            "cogs.autonomous_moderation.moderator.budget_reset_time",
                            locale=guild_locale,
                            placeholders={
                                "time": cycle_end.strftime('%Y-%m-%d %H:%M UTC')
                            },
                            fallback=cycle_end.strftime('%Y-%m-%d %H:%M UTC'),
                            guild_id=gid,
                        )
                        if cycle_end
                        else self.bot.translate(
                            "cogs.autonomous_moderation.moderator.budget_reset_next",
                            locale=guild_locale,
                            fallback="the next cycle",
                            guild_id=gid,
                        )
                    )
                    await trigger_msg.reply(
                        self.bot.translate(
                            "cogs.autonomous_moderation.moderator.budget_reached",
                            locale=guild_locale,
                            placeholders={"reset": reset_label},
                            fallback=(
                                "AI moderation budget reached for this billing cycle. "
                                f"Resets at {reset_label}."
                            ),
                            guild_id=gid,
                        ),
                        mention_author=False,
                    )
                except discord.HTTPException:
                    pass
                # Keep batches so we can try again next cycle
                continue

            aimod_debug = settings.get("aimod-debug") or False
            ai_channel_id = settings.get("aimod-channel")
            monitor_channel_id = settings.get("monitor-channel")
            debug_log_channel = ai_channel_id or monitor_channel_id

            if not report or not report.violations:
                if trigger_msg:
                    try:
                        await trigger_msg.reply(
                            self.bot.translate(
                                "cogs.autonomous_moderation.moderator.thanks_no_violation",
                                locale=guild_locale,
                                fallback="Thanks for the report. No violations were found.",
                                guild_id=gid,
                            )
                        )
                    except discord.HTTPException:
                        pass
                # Always log to AI violations channel when debug is enabled
                if aimod_debug and debug_log_channel:
                    mode_str = await get_active_mode(gid)
                    embed = am_helpers.build_no_violations_embed(
                        self.bot, guild, len(batch), mode_str
                    )
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

                out_reason, out_rule = am_helpers.summarize_reason_rule(
                    self.bot, guild, data.get("reasons"), data.get("rules")
                )

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
                    await trigger_msg.reply(
                        self.bot.translate(
                            "cogs.autonomous_moderation.moderator.thanks_action",
                            locale=guild_locale,
                            fallback="Thanks for the report. Action was taken.",
                            guild_id=gid,
                        )
                    )
                except discord.HTTPException:
                    pass

async def setup_autonomous(bot: commands.Bot):
    await bot.add_cog(AutonomousModeratorCog(bot))
