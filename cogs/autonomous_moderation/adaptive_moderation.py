import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta, timezone, time as dt_time
from collections import defaultdict

from modules.utils.mysql import get_settings, update_settings

MASS_JOIN_WINDOW = timedelta(minutes=1)
MASS_LEAVE_WINDOW = timedelta(minutes=1)
CHANNEL_SPIKE_WINDOW = timedelta(seconds=30)
CHANNEL_SPIKE_THRESHOLD = 10
SERVER_SPIKE_WINDOW = timedelta(seconds=30)
SERVER_SPIKE_THRESHOLD = 30
GUILD_INACTIVE_WINDOW = timedelta(minutes=30)

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
            joins = [t for t in self.joins[gid] if now - t <= MASS_JOIN_WINDOW]
            if len(joins) >= 5:
                if "mass_join" in settings:
                    await apply_adaptive_actions(guild, settings["mass_join"])
                self.joins[gid] = []

            # Mass Leave
            leaves = [t for t in self.leaves[gid] if now - t <= MASS_LEAVE_WINDOW]
            if len(leaves) >= 5:
                if "mass_leave" in settings:
                    await apply_adaptive_actions(guild, settings["mass_leave"])
                self.leaves[gid] = []

            # Inactive Server
            last_time = self.last_message_time.get(gid)
            if last_time and now - last_time > GUILD_INACTIVE_WINDOW:
                if "guild_inactive" in settings:
                    await apply_adaptive_actions(guild, settings["guild_inactive"])

            # Channel Spikes
            for (g_id, c_id), timestamps in list(self.channel_activity.items()):
                if g_id != gid:
                    continue
                recent = [t for t in timestamps if now - t <= CHANNEL_SPIKE_WINDOW]
                if len(recent) >= CHANNEL_SPIKE_THRESHOLD:
                    key = f"channel_spike:{c_id}"
                    if key in settings:
                        await apply_adaptive_actions(guild, settings[key])
                    self.channel_activity[(g_id, c_id)] = []

            # Server Spike
            server_recent_msgs = 0
            for (g_id, _), timestamps in self.channel_activity.items():
                if g_id != gid:
                    continue
                server_recent_msgs += sum(1 for t in timestamps if now - t <= SERVER_SPIKE_WINDOW)

            if server_recent_msgs >= SERVER_SPIKE_THRESHOLD:
                if "server_spike" in settings:
                    await apply_adaptive_actions(guild, settings["server_spike"])
                    for key in list(self.channel_activity.keys()):
                        if key[0] == gid:
                            self.channel_activity[key] = []

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
    current_mode = await get_settings(guild.id, "aimod-active-mode") or "report"

    for action in actions:
        if action == "enable_interval" and current_mode != "interval":
            await update_settings(guild.id, "aimod-active-mode", "interval")
            current_mode = "interval"

        elif action == "disable_interval" and current_mode == "interval":
            await update_settings(guild.id, "aimod-active-mode", "report")
            current_mode = "report"

        elif action == "enable_report" and current_mode != "report":
            await update_settings(guild.id, "aimod-active-mode", "report")
            current_mode = "report"

        elif action == "disable_report" and current_mode == "report":
            await update_settings(guild.id, "aimod-active-mode", "interval")
            current_mode = "interval"

async def setup_adaptive(bot: commands.Bot):
    await bot.add_cog(AdaptiveModerationCog(bot))