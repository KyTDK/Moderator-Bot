import tracemalloc
import psutil
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import os
import platform
import time
import sys

load_dotenv()
GUILD_ID = int(os.getenv('GUILD_ID', 0))
ALLOWED_USER_IDS = [int(id) for id in os.getenv('ALLOWED_USER_IDS', '').split(',') if id.isdigit()]

# Start tracemalloc to track Python-level allocations
tracemalloc.start()

def _mb(b: int) -> float:
    return (b or 0) / 1024 / 1024

def _shorten_path(path: str, project_root: str) -> str:
    if not path:
        return "[anonymous]"
    try:
        abs_root = os.path.abspath(project_root)
        abs_path = os.path.abspath(path)
        if abs_path.startswith(abs_root):
            return os.path.relpath(abs_path, abs_root)
    except Exception:
        pass
    # Fall back to basename to keep lines short
    return os.path.basename(path) or path

class DebugCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.process = psutil.Process()
        self.start_time = time.time()

    @app_commands.command(name="stats", description="Get memory and performance stats")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.describe(show_all="Include allocations from all libraries (not just project)")
    @app_commands.checks.has_permissions(administrator=True)
    async def stats(self, interaction: discord.Interaction, show_all: bool = True):
        await interaction.response.defer(ephemeral=True)

        # Check if user is allowed
        if interaction.user.id not in ALLOWED_USER_IDS:
            await interaction.followup.send(
                "You do not have permission to use this command.",
                ephemeral=True
            )
            return

        # ---- Python (tracemalloc) ----
        current, peak = tracemalloc.get_traced_memory()
        current_mb = _mb(current)
        peak_mb = _mb(peak)

        snapshot = tracemalloc.take_snapshot()
        top_stats = snapshot.statistics("lineno")

        project_root = os.getcwd()
        py_top_lines = []
        for stat in top_stats:
            frame = stat.traceback[0]
            filename = frame.filename or "<unknown>"
            # Respect show_all for Python files
            if not show_all and not os.path.abspath(filename).startswith(os.path.abspath(project_root)):
                continue
            short = _shorten_path(filename, project_root)
            avg_size = (stat.size // stat.count) if stat.count else 0
            line = (
                f"{len(py_top_lines)+1}. {short}:{frame.lineno} - "
                f"size={stat.size/1024:.1f} KiB, count={stat.count}, avg={avg_size} B"
            )
            py_top_lines.append(line)
            if len(py_top_lines) >= 10:
                break
        if not py_top_lines:
            py_top_lines.append("No Python allocations matched the filter.")

        rss = _mb(self.process.memory_info().rss)
        vms = _mb(self.process.memory_info().vms)

        uss_mb = pss_mb = None
        try:
            full = self.process.memory_full_info()
            uss_mb = _mb(getattr(full, "uss", 0))
            pss_mb = _mb(getattr(full, "pss", 0)) if hasattr(full, "pss") else None
        except (psutil.AccessDenied, AttributeError):
            pass

        # Build native memory map summary (grouped by path)
        native_lines = []
        try:
            mmaps = self.process.memory_maps(grouped=True)
            # Aggregate per path
            agg = {}
            for m in mmaps:
                path = m.path or ""
                if not show_all:
                    if path and not os.path.abspath(path).startswith(os.path.abspath(project_root)):
                        continue
                entry = agg.setdefault(path, {"rss": 0, "private": 0, "swap": 0})
                entry["rss"] += getattr(m, "rss", 0)
                entry["private"] += getattr(m, "private", 0)
                entry["swap"] += getattr(m, "swap", 0)

            # Sort by RSS desc
            top = sorted(agg.items(), key=lambda kv: kv[1]["rss"], reverse=True)[:10]
            for i, (path, stats) in enumerate(top, 1):
                line = (
                    f"{i}. {_shorten_path(path, project_root)} - "
                    f"rss={_mb(stats['rss']):.2f} MB, priv={_mb(stats['private']):.2f} MB, swap={_mb(stats['swap']):.2f} MB"
                )
                native_lines.append(line)

            if not native_lines:
                native_lines.append("No native mappings matched the filter.")
        except psutil.AccessDenied:
            native_lines.append("Access denied reading memory maps.")
        except Exception as e:
            native_lines.append(f"Error reading memory maps: {type(e).__name__}")

        # CPU, uptime, threads, handles/FDs
        cpu_percent = self.process.cpu_percent(interval=0.5)
        uptime = time.time() - self.start_time
        uptime_str = time.strftime("%H:%M:%S", time.gmtime(uptime))
        threads = self.process.num_threads()
        handles = "N/A"
        if hasattr(self.process, "num_handles"):
            try:
                handles = self.process.num_handles()
            except psutil.AccessDenied:
                handles = "AccessDenied"
        elif hasattr(self.process, "num_fds"):
            try:
                handles = f"FDs: {self.process.num_fds()}"
            except psutil.AccessDenied:
                handles = "FDs: AccessDenied"

        def chunk_lines(lines, max_chunk_len=900):
            chunks, current_chunk = [], ""
            for line in lines:
                if len(current_chunk) + len(line) + 1 > max_chunk_len:
                    chunks.append(current_chunk)
                    current_chunk = line
                else:
                    current_chunk += ("\n" if current_chunk else "") + line
            if current_chunk:
                chunks.append(current_chunk)
            return chunks

        py_chunks = chunk_lines(py_top_lines)
        native_chunks = chunk_lines(native_lines)

        # Build embed
        embed = discord.Embed(
            title="Bot Performance Stats",
            color=discord.Color.blurple()
        )

        mem_lines = [
            f"**RSS (Actual):** {rss:.2f} MB",
            f"**VMS (Virtual):** {vms:.2f} MB",
            f"**Tracemalloc Current:** {current_mb:.2f} MB",
            f"**Tracemalloc Peak:** {peak_mb:.2f} MB",
        ]
        if uss_mb is not None:
            mem_lines.append(f"**USS (Unique):** {uss_mb:.2f} MB")
        if pss_mb is not None:
            mem_lines.append(f"**PSS (Proportional):** {pss_mb:.2f} MB")

        embed.add_field(name="Memory Usage", value="\n".join(mem_lines), inline=False)
        embed.add_field(
            name="CPU & Threads",
            value=(f"**CPU Usage:** {cpu_percent:.1f}%\n"
                   f"**Threads:** {threads}\n"
                   f"**Handles:** {handles}"),
            inline=False
        )
        embed.add_field(
            name="Bot Stats",
            value=(f"**Guilds:** {len(self.bot.guilds)}\n"
                   f"**Users (cached):** {len(self.bot.users)}\n"
                   f"**Uptime:** {uptime_str}"),
            inline=False
        )

        for i, chunk in enumerate(py_chunks, 1):
            embed.add_field(
                name=f"Top Python Allocations {i}",
                value=f"```{chunk}```",
                inline=False
            )

        for i, chunk in enumerate(native_chunks, 1):
            embed.add_field(
                name=f"Top Native Memory Maps {i}",
                value=f"```{chunk}```",
                inline=False
            )

        embed.set_footer(text=f"Host: {platform.node()} | Python {platform.python_version()}")

        await interaction.followup.send(embed=embed, ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(DebugCog(bot))
    if GUILD_ID:
        await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
