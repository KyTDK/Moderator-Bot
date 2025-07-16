import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta, timezone, time as dt_time
from collections import defaultdict

from modules.utils.mysql import get_settings, update_settings

MASS_JOIN_WINDOW = timedelta(minutes=1)
MASS_LEAVE_WINDOW = timedelta(minutes=1)
SERVER_SPIKE_WINDOW = timedelta(seconds=30)
SERVER_SPIKE_THRESHOLD = 30

class AdaptiveModerationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.joins: defaultdict[int, list[datetime]] = defaultdict(list)
        self.leaves: defaultdict[int, list[datetime]] = defaultdict(list)
        self.channel_activity: defaultdict[tuple[int, int], list[datetime]] = defaultdict(list)  # (guild_id, channel_id)
        self.last_message_time: dict[int, datetime] = {}
        self.monitor_loop.start()

    def cog_unload(self):
        self.monitor_loop.cancel()

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        self.joins[member.guild.id].append(datetime.now(timezone.utc))

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        self.leaves[member.guild.id].append(datetime.now(timezone.utc))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        now = datetime.now(timezone.utc)
        self.channel_activity[(message.guild.id, message.channel.id)].append(now)
        self.last_message_time[message.guild.id] = now

    @commands.Cog.listener()
    async def on_presence_update(self, before: discord.Member, after: discord.Member):
        if not after.guild:
            return

        if await get_settings(before.guild.id, "aimod-mode") != "adaptive":
            return

        before_online = before.status != discord.Status.offline
        after_online = after.status != discord.Status.offline
        if before_online == after_online:
            return

        settings = await get_settings(after.guild.id, "aimod-adaptive-events") or {}

        for event_string, actions in settings.items():
            if ":" not in event_string:
                continue
            event_type, role_ids_raw = event_string.split(":", 1)
            role_ids = {int(rid) for rid in role_ids_raw.split(",") if rid.isdigit()}
            member_roles = {role.id for role in after.roles}
            if not (member_roles & role_ids):
                continue

            relevant_members = [
                m for m in after.guild.members if any(role.id in role_ids for role in m.roles)
            ]

            if event_type == "role_online":
                if all(m.status != discord.Status.offline for m in relevant_members):
                    await apply_adaptive_actions(after.guild, actions)
            elif event_type == "role_offline":
                if all(m.status == discord.Status.offline for m in relevant_members):
                    await apply_adaptive_actions(after.guild, actions)
            elif event_type.startswith("role_online_percent"):
                try:
                    _, role_ids_raw, threshold_raw = event_string.split(":")
                    threshold = float(threshold_raw)
                except ValueError:
                    continue  # malformed entry

                online = sum(1 for m in relevant_members if m.status != discord.Status.offline)
                total = len(relevant_members)
                if total > 0 and online / total >= threshold:
                    await apply_adaptive_actions(after.guild, actions)


    @tasks.loop(seconds=30)
    async def monitor_loop(self):
        now = datetime.now(timezone.utc)
        for guild in self.bot.guilds:
            gid = guild.id
            if await get_settings(gid, "aimod-mode") != "adaptive":
                continue

            settings = await get_settings(gid, "aimod-adaptive-events") or {}

            # Mass Join
            recent_joins = [t for t in self.joins[gid] if now - t <= MASS_JOIN_WINDOW]
            if len(recent_joins) >= 5:
                if "mass_join" in settings:
                    await apply_adaptive_actions(guild, settings["mass_join"])
                self.joins[gid] = []
            else:
                self.joins[gid] = recent_joins

            # Mass Leave
            recent_leaves = [t for t in self.leaves[gid] if now - t <= MASS_LEAVE_WINDOW]
            if len(recent_leaves) >= 5:
                if "mass_leave" in settings:
                    await apply_adaptive_actions(guild, settings["mass_leave"])
                self.leaves[gid] = []
            else:
                self.leaves[gid] = recent_leaves

            # Server Spike (relative comparison)
            recent_count = 0
            previous_count = 0
            for (g_id, _), timestamps in self.channel_activity.items():
                if g_id != gid:
                    continue
                for t in timestamps:
                    delta = now - t
                    if delta <= SERVER_SPIKE_WINDOW:
                        recent_count += 1
                    elif SERVER_SPIKE_WINDOW < delta <= SERVER_SPIKE_WINDOW * 1.3:
                        previous_count += 1

            print(f"[DEBUG] Guild {gid}: recent={recent_count}, previous={previous_count}")

            # Compute scaling multiplier threshold
            if previous_count > 0:
                required_multiplier = max(1.2, min(3.0, 3.0 / (previous_count ** 0.3)))
                print(f"[DEBUG] Guild {gid}: multiplier required = {required_multiplier:.2f}")
                
                if recent_count >= previous_count * required_multiplier:
                    print(f"[DEBUG] Guild {gid}: server_spike triggered by scaled multiplier")
                    if "server_spike" in settings:
                        await apply_adaptive_actions(guild, settings["server_spike"])
                        for key in list(self.channel_activity.keys()):
                            if key[0] == gid:
                                self.channel_activity[key] = []

                elif recent_count < previous_count / required_multiplier:
                    print(f"[DEBUG] Guild {gid}: server_inactive triggered by scaled dropoff")
                    if "guild_inactive" in settings:
                        await apply_adaptive_actions(guild, settings["guild_inactive"])

            # Time-based
            current_utc = now.time()
            for key, actions in settings.items():
                if key.startswith("time_range:"):
                    _, time_str = key.split(":", 1)
                    try:
                        start_str, end_str = time_str.split("-")
                        start_time = dt_time.fromisoformat(start_str)
                        end_time = dt_time.fromisoformat(end_str)
                        if start_time <= current_utc <= end_time:
                            await apply_adaptive_actions(guild, actions)
                    except ValueError:
                        continue

async def apply_adaptive_actions(guild: discord.Guild, actions: list[str]):
    mode = await get_settings(guild.id, "aimod-active-mode") or "report"
    target_mode = mode

    interval_triggers = {"enable_interval", "disable_report"}
    report_triggers = {"enable_report", "disable_interval"}

    for action in actions:
        if action in interval_triggers:
            target_mode = "interval"
        elif action in report_triggers:
            target_mode = "report"

    if target_mode != mode:
        await update_settings(guild.id, "aimod-active-mode", target_mode)

async def setup_adaptive(bot: commands.Bot):
    await bot.add_cog(AdaptiveModerationCog(bot))