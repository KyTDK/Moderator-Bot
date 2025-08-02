import tracemalloc
import psutil
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import os
import platform
import time

load_dotenv()
GUILD_ID = int(os.getenv('GUILD_ID', 0))
ALLOWED_USER_IDS = [int(id) for id in os.getenv('ALLOWED_USER_IDS', '').split(',') if id.isdigit()]

# Start tracemalloc to track memory
tracemalloc.start()

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

        # Check if bot is in list of allowed user IDs
        if interaction.user.id not in ALLOWED_USER_IDS:
            await interaction.followup.send(
                "You do not have permission to use this command.",
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
            top_allocations.append("No project-related allocations found.")

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
        embed = discord.Embed(
            title="Bot Performance Stats",
            color=discord.Color.blurple()
        )
        embed.add_field(
            name="Memory Usage",
            value=(
                f"**RSS (Actual):** {rss:.2f} MB\n"
                f"**VMS (Virtual):** {vms:.2f} MB\n"
                f"**Tracemalloc Current:** {current_mb:.2f} MB\n"
                f"**Tracemalloc Peak:** {peak_mb:.2f} MB"
            ),
            inline=False
        )
        embed.add_field(
            name="CPU & Threads",
            value=(
                f"**CPU Usage:** {cpu_percent:.1f}%\n"
                f"**Threads:** {threads}\n"
                f"**Handles:** {handles}"
            ),
            inline=False
        )
        embed.add_field(
            name="Bot Stats",
            value=(
                f"**Guilds:** {len(self.bot.guilds)}\n"
                f"**Users (cached):** {len(self.bot.users)}\n"
                f"**Uptime:** {uptime_str}"
            ),
            inline=False
        )

        for i, chunk in enumerate(chunks, 1):
            embed.add_field(
                name=f"Top Memory Allocations {i}",
                value=f"```{chunk}```",
                inline=False
            )

        embed.set_footer(text=f"Host: {platform.node()} | Python {platform.python_version()}")

        await interaction.followup.send(embed=embed, ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(DebugCog(bot))
    if GUILD_ID:
        await bot.tree.sync(guild=discord.Object(id=GUILD_ID))