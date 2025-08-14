from discord.ext import commands
from discord import app_commands, Interaction
from modules.moderation import strike
from modules.utils.action_manager import ActionListManager
from modules.utils.mysql import execute_query
import io
import discord
from modules.utils import mod_logging, mysql
from modules.utils.strike import validate_action
from modules.utils.actions import action_choices, VALID_ACTION_VALUES
from modules.utils.url_utils import extract_urls, norm_domain, norm_url
from urllib.parse import urlparse

MAX_URLS = 500
BANNEDURLS_ACTION_SETTING = "url-detection-action"
manager = ActionListManager(BANNEDURLS_ACTION_SETTING)

def _ensure_http(u: str) -> str:
    return u if u.startswith(("http://","https://")) else f"http://{u}"

def _is_domain_only(s: str) -> bool:
    p = urlparse(_ensure_http(s))
    return (p.path or "").strip("/") == "" and not p.query and not p.fragment

class BannedURLsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
    
    bannedurls_group = app_commands.Group(
        name="bannedurls",
        description="Banned URLs management commands.",
        default_permissions=discord.Permissions(manage_messages=True),
        guild_only=True
    )

    @bannedurls_group.command(name="add", description="Add a URL or domain to the banned list.")
    async def add_banned_urls(self, interaction: Interaction, url: str):
        guild_id = interaction.guild.id

        # limit
        count_row, _ = await execute_query(
            "SELECT COUNT(*) FROM banned_urls WHERE guild_id = %s", (guild_id,), fetch_one=True
        )
        if count_row and count_row[0] >= MAX_URLS:
            await interaction.response.send_message(
                f"You've reached the limit of {MAX_URLS} banned URLs for this server.\n"
                f"Use `/bannedurls remove` or `/bannedurls clear` to make space.",
                ephemeral=True
            )
            return
        
        # already present?
        row, _ = await execute_query(
            "SELECT 1 FROM banned_urls WHERE guild_id = %s AND url = %s",
            (guild_id, url),
            fetch_one=True
        )
        if row:
            await interaction.response.send_message(f"`{url}` is already banned.", ephemeral=True)
            return

        await execute_query(
            "INSERT INTO banned_urls (guild_id, url) VALUES (%s, %s)",
            (guild_id, url)
        )
        await interaction.response.send_message(f"Added `{url}` to the banned list.", ephemeral=True)

    @bannedurls_group.command(name="remove", description="Remove a URL or domain from the banned list.")
    async def remove_banned_urls(self, interaction: Interaction, url: str):
        guild_id = interaction.guild.id

        _, affected = await execute_query(
            "DELETE FROM banned_urls WHERE guild_id = %s AND url = %s",
            (guild_id, url)
        )
        if affected == 0:
            await interaction.response.send_message(f"`{url}` is not in the banned list.", ephemeral=True)
        else:
            await interaction.response.send_message(f"Removed `{url}` from the banned list.", ephemeral=True)

    @remove_banned_urls.autocomplete("url")
    async def banned_urls_autocomplete(self, interaction: Interaction, current: str) -> list[app_commands.Choice[str]]:
        guild_id = interaction.guild.id
        rows, _ = await execute_query(
            "SELECT url FROM banned_urls WHERE guild_id = %s", (guild_id,), fetch_all=True
        )
        all_urls = [r[0] for r in (rows or []) if r]
        filtered = [u for u in all_urls if current.lower() in u.lower()]
        return [app_commands.Choice(name=u, value=u) for u in filtered[:25]]

    @bannedurls_group.command(name="list", description="List all banned URLs.")
    async def list_banned_urls(self, interaction: Interaction):
        guild_id = interaction.guild.id
        rows, _ = await execute_query(
            "SELECT url FROM banned_urls WHERE guild_id = %s", (guild_id,), fetch_all=True
        )
        banned = [row[0] for row in (rows or [])]
        if not banned:
            await interaction.response.send_message("No banned URLs found.", ephemeral=True)
            return
        buf = io.StringIO("Banned URLs:\n" + "\n".join(f"- {u}" for u in banned))
        file = discord.File(buf, filename=f"banned_urls_{interaction.guild.id}.txt")
        await interaction.response.send_message(file=file, ephemeral=True)

    @bannedurls_group.command(name="clear", description="Clear all banned URLs.")
    async def clear_banned_urls(self, interaction: Interaction):
        guild_id = interaction.guild.id
        _, affected = await execute_query("DELETE FROM banned_urls WHERE guild_id = %s", (guild_id,))
        if affected == 0:
            await interaction.response.send_message("No banned URLs found to clear.", ephemeral=True)
            return
        await interaction.response.send_message("All banned URLs have been cleared.", ephemeral=True)

    async def handle_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        guild_id = message.guild.id

        # Exclude channels
        if message.channel.id in [int(c) for c in (await mysql.get_settings(guild_id, "exclude-url-channels") or [])]:
            return

        # Load banned
        rows, _ = await execute_query(
            "SELECT url FROM banned_urls WHERE guild_id = %s", (guild_id,), fetch_all=True
        )
        banned = [r[0] for r in (rows or []) if r and r[0]]
        if not banned:
            return

        raw_domains = [u for u in banned if _is_domain_only(u)]
        raw_exact   = [u for u in banned if not _is_domain_only(u)]

        banned_domains_norm = {norm_domain(u) for u in raw_domains}
        banned_exact_norm   = {norm_url(u)    for u in raw_exact}

        # Extract URLs from the message
        extracted = extract_urls(message.content)
        if not extracted:
            return

        def _domain_hits(msg_domain: str) -> bool:
            return any(
                msg_domain == d or msg_domain.endswith("." + d)
                for d in banned_domains_norm
            )

        matched_url = None
        for u in extracted:
            if norm_url(u) in banned_exact_norm or _domain_hits(norm_domain(u)):
                matched_url = u
                break

        if not matched_url:
            return

        action_flag = await mysql.get_settings(guild_id, BANNEDURLS_ACTION_SETTING)
        if action_flag:
            try:
                await strike.perform_disciplinary_action(
                    user=message.author,
                    bot=self.bot,
                    action_string=action_flag,
                    reason=f"Message contained banned URL ({matched_url})",
                    source="banned url",
                    message=message,
                )
            except Exception:
                pass

        try:
            embed = discord.Embed(
                title="Banned URL Detected",
                description=(
                    f"{message.author.mention}, your message was removed because it contained a banned URL.\n"
                    f"**URL:** {matched_url}"
                ),
                color=discord.Color.red(),
            )
            embed.set_thumbnail(url=message.author.display_avatar.url)
            await mod_logging.log_to_channel(
                embed=embed,
                channel_id=message.channel.id,
                bot=self.bot,
            )
        except discord.Forbidden:
            pass

    async def handle_message_edit(self, cached_before: dict, after: discord.Message):
        await self.handle_message(after)

    @bannedurls_group.command(name="add_action", description="Add a moderation action to be triggered when a banned URL is detected.")
    @app_commands.describe(action="Action to perform", duration="Only required for timeout (e.g. 10m, 1h, 3d)")
    @app_commands.choices(action=action_choices())
    async def add_banned_action(self, interaction: Interaction, action: str, duration: str = None, role: discord.Role = None, reason: str = None):
        await interaction.response.defer(ephemeral=True)
        action_str = await validate_action(interaction=interaction, action=action, duration=duration, role=role, valid_actions=VALID_ACTION_VALUES, param=reason)
        if action_str is None:
            return
        msg = await manager.add_action(interaction.guild.id, action_str)
        await interaction.followup.send(msg, ephemeral=True)

    @bannedurls_group.command(name="remove_action", description="Remove a specific action from the list of punishments for banned URLs.")
    @app_commands.describe(action="Exact action string to remove (e.g. timeout, delete)")
    @app_commands.autocomplete(action=manager.autocomplete)
    async def remove_banned_action(self, interaction: Interaction, action: str):
        msg = await manager.remove_action(interaction.guild.id, action)
        await interaction.response.send_message(msg, ephemeral=True)

    @bannedurls_group.command(name="view_actions", description="Show all actions currently configured to trigger when banned URLs are used.")
    async def view_banned_actions(self, interaction: Interaction):
        actions = await manager.view_actions(interaction.guild.id)
        if not actions:
            await interaction.response.send_message("No actions are currently set for banned URLs.", ephemeral=True)
            return
        formatted = "\n".join(f"{i+1}. `{a}`" for i, a in enumerate(actions))
        await interaction.response.send_message(f"**Current banned URLs actions:**\n{formatted}", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(BannedURLsCog(bot))
