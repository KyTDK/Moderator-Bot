import tracemalloc
import psutil
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import os
import platform
import time
from modules.core.moderator_bot import ModeratorBot
from modules.i18n.strings import locale_string

load_dotenv()
GUILD_ID = int(os.getenv('GUILD_ID', 0))
ALLOWED_USER_IDS = [int(id) for id in os.getenv('ALLOWED_USER_IDS', '').split(',') if id.isdigit()]

# Start tracemalloc to track memory
tracemalloc.start()

class DebugCog(commands.Cog):
    def __init__(self, bot: ModeratorBot):
        self.bot = bot
        self.process = psutil.Process()
        self.start_time = time.time()

    @app_commands.command(
        name="stats",
        description=locale_string("cogs.debug.meta.stats.description"),
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.describe(
        show_all=locale_string("cogs.debug.meta.stats.show_all")
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def stats(self, interaction: discord.Interaction, show_all: bool = True):
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild.id
        # Check if bot is in list of allowed user IDs
        if interaction.user.id not in ALLOWED_USER_IDS:
            await interaction.followup.send(
                self.bot.translate("cogs.debug.permission_denied",
                                   guild_id=guild_id
                                   ),
                ephemeral=True
            )
            return

        # Get current and peak memory usage
        current, peak = tracemalloc.get_traced_memory()
        current_mb = current / 1024 / 1024
        peak_mb = peak / 1024 / 1024

        # Memory info
        rss = self.process.memory_info().rss / 1024 / 1024
        vms = self.process.memory_info().vms / 1024 / 1024

        # CPU and uptime
        cpu_percent = self.process.cpu_percent(interval=0.5)
        uptime = time.time() - self.start_time
        uptime_str = time.strftime("%H:%M:%S", time.gmtime(uptime))

        # Threads and handles
        threads = self.process.num_threads()
        handles = self.process.num_handles() if hasattr(self.process, "num_handles") else "N/A"

        # Get top memory allocations
        snapshot = tracemalloc.take_snapshot()
        top_stats = snapshot.statistics("lineno")

        project_root = os.getcwd()
        top_allocations = []
        for stat in top_stats:
            frame = stat.traceback[0]
            filename = frame.filename

            # Skip files outside the project if show_all is False
            if not show_all and not filename.startswith(project_root):
                continue

            if filename.startswith(project_root):
                filename = os.path.relpath(filename, project_root)

            formatted = f"{filename}:{frame.lineno}"
            avg_size = stat.size // stat.count if stat.count else 0
            line = f"{len(top_allocations)+1}. {formatted} - size={stat.size / 1024:.1f} KiB, count={stat.count}, avg={avg_size} B"
            top_allocations.append(line)

            if len(top_allocations) >= 10:  # Limit to top 10
                break
        
        if not top_allocations:
            top_allocations.append(self.bot.translate("cogs.debug.no_allocations",
                                                      guild_id=guild_id))

        chunks = []
        current_chunk = ""
        for line in top_allocations:
            if len(current_chunk) + len(line) + 1 > 900:
                chunks.append(current_chunk)
                current_chunk = line
            else:
                current_chunk += ("\n" if current_chunk else "") + line
        if current_chunk:
            chunks.append(current_chunk)

        # Build embed
        debug_texts = self.bot.translate("cogs.debug.embed",
                                         guild_id=guild_id)
        embed = discord.Embed(
            title=debug_texts["title"],
            color=discord.Color.blurple()
        )
        embed.add_field(
            name=debug_texts["memory_name"],
            value=debug_texts["memory_value"].format(rss=rss, vms=vms, current_mb=current_mb, peak_mb=peak_mb),
            inline=False
        )
        embed.add_field(
            name=debug_texts["cpu_name"],
            value=debug_texts["cpu_value"].format(cpu_percent=cpu_percent, threads=threads, handles=handles),
            inline=False
        )
        embed.add_field(
            name=debug_texts["bot_name"],
            value=debug_texts["bot_value"].format(guilds=len(self.bot.guilds), users=len(self.bot.users), uptime=uptime_str),
            inline=False
        )

        for i, chunk in enumerate(chunks, 1):
            embed.add_field(
                name=debug_texts["allocations_name"].format(index=i),
                value=f"```{chunk}```",
                inline=False
            )

        embed.set_footer(text=debug_texts["footer"].format(host=platform.node(), python_version=platform.python_version()))

        # Worker queue backlogs and autoscale info
        try:
            queue_lines: list[str] = []
            def fmt_line(cog_name: str, queue_name: str, q) -> str:
                m = getattr(q, "metrics", None)
                data = m() if callable(m) else None
                if not data:
                    # Fallback minimal info
                    backlog = getattr(getattr(q, "queue", None), "qsize", lambda: "?")()
                    workers = len(getattr(q, "workers", []))
                    maxw = getattr(q, "max_workers", "?")
                    return f"[{cog_name}:{queue_name}] backlog={backlog} workers={workers}/{maxw}"
                def _int(value, default=0):
                    try:
                        if value is None:
                            return default
                        return int(value)
                    except (TypeError, ValueError):
                        return default

                def _float(value, default=0.0):
                    try:
                        if value is None:
                            return default
                        return float(value)
                    except (TypeError, ValueError):
                        return default

                backlog = _int(data.get("backlog"))
                max_workers = max(1, _int(data.get("max_workers"), 1))
                busy = _int(data.get("busy_workers"), _int(data.get("active_workers")))
                baseline = max(1, _int(data.get("baseline_workers"), 1))
                burst = _int(data.get("autoscale_max"), max_workers)
                hi_value = data.get("backlog_high")
                lo_value = data.get("backlog_low")
                hi = str(_int(hi_value)) if hi_value is not None else "-"
                lo = str(_int(lo_value)) if lo_value is not None else "-"
                pending = _int(data.get("pending_stops"))
                tasks_completed = _int(data.get("tasks_completed"))
                dropped = _int(data.get("dropped_tasks_total"))
                limit = None
                hard_limit_value = data.get("backlog_hard_limit")
                if hard_limit_value is not None:
                    hard_limit = _int(hard_limit_value)
                    shed_to_value = data.get("backlog_shed_to")
                    if shed_to_value is not None:
                        limit = f"{hard_limit}->{_int(shed_to_value)}"
                    else:
                        limit = str(hard_limit)
                wait_avg = _float(data.get("avg_wait_time"))
                wait_last = _float(data.get("last_wait_time"))
                wait_long = _float(data.get("longest_wait"))
                run_avg = _float(data.get("avg_runtime"))
                run_last = _float(data.get("last_runtime"))
                run_long = _float(data.get("longest_runtime"))
                running_flag = bool(data.get("running"))
                parts = [
                    f"[{cog_name}:{queue_name}]",
                    f"backlog={backlog}",
                    f"busy={busy}/{max_workers}",
                    f"base={baseline}",
                    f"burst={burst}",
                    f"hi={hi}",
                    f"lo={lo}",
                    f"pend={pending}",
                    f"tasks={tasks_completed}",
                    f"drop={dropped}",
                    f"wait={wait_avg:.2f}|{wait_last:.2f}|{wait_long:.2f}",
                    f"run={run_avg:.2f}|{run_last:.2f}|{run_long:.2f}",
                    f"running={running_flag}",
                ]
                if limit is not None:
                    parts.insert(7, f"limit={limit}")
                return " ".join(parts)

            for cog_name in ("AggregatedModerationCog", "EventDispatcherCog", "ScamDetectionCog"):
                cog = self.bot.get_cog(cog_name)
                if not cog:
                    continue
                for qname in ("free_queue", "accelerated_queue"):
                    q = getattr(cog, qname, None)
                    if q is not None:
                        queue_lines.append(fmt_line(cog_name, qname.replace("_queue", ""), q))

            if queue_lines:
                embed.add_field(
                    name=debug_texts["worker_name"],
                    value=f"```\n" + "\n".join(queue_lines) + "\n```",
                    inline=False,
                )
        except Exception as e:
            embed.add_field(name=debug_texts["worker_name"], value=debug_texts["worker_error"].format(error=e), inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="locale",
        description=locale_string("cogs.debug.meta.locale.description"),
    )
    async def current_locale(self, interaction: discord.Interaction):
        current = self.bot.current_locale()
        fallback = self.bot.translator.default_locale
        locale_texts = self.bot.translate(
            "cogs.debug.locale",
            guild_id=interaction.guild.id if interaction.guild else None,
        )
        message = locale_texts["current"].format(locale=current or fallback)
        await interaction.response.send_message(message, ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(DebugCog(bot))
    if GUILD_ID:
        if isinstance(bot, ModeratorBot):
            await bot.ensure_command_tree_translator()
        await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
