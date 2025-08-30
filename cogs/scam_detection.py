import json
import os
import discord
from discord.ext import commands
from discord import app_commands, Interaction
from dotenv import load_dotenv
from modules.utils import mod_logging
from modules.utils.mysql import execute_query, get_settings, update_settings
from modules.moderation import strike
from modules.utils.action_manager import ActionListManager
from modules.utils.strike import validate_action
from modules.utils.actions import action_choices, VALID_ACTION_VALUES
from modules.utils.text import normalize_text
import aiohttp
from discord.ext import tasks
from modules.utils.url_utils import extract_urls, unshorten_url, update_tld_list
from modules.worker_queue import WorkerQueue

load_dotenv()
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
ACTION_SETTING = "scam-detection-action"
EXCLUDE_CHANNELS_SETTING = "exclude-scam-channels"
CHECK_LINKS_SETTING = "check-links"

manager = ActionListManager(ACTION_SETTING)

PHISHTANK_URL = "http://data.phishtank.com/data/online-valid.json"
PHISHTANK_CACHE_FILE = "phishtank_cache.json"
PHISHTANK_USER_AGENT = {"User-Agent": "ModeratorBot/1.0"}

SAFE_URLS = [
    "discord.com",
    "youtube.com",
    "google.com",
    "reddit.com",
    "github.com",
    "twitter.com",
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "wikipedia.org",
    "stackoverflow.com",
    "medium.com",
    "amazon.com",
    "apple.com",
    "microsoft.com",
    "tiktok.com",
    "netflix.com",
    "paypal.com",
    "docs.google.com",
    "drive.google.com",
    "dropbox.com",
    "vercel.app",
    "notion.so",
    "openai.com",
    "cloudflare.com",
    "tenor.com",
    "giphy.com",
    "cdn.discordapp.com",
    "discord.gg",
    "discordapp.com",
    "steamcommunity.com",
    "store.steampowered.com",
    "roblox.com",
    "media.discordapp.net",
    "twitch.tv",
    "youtu.be",
    "spotify.com",
    "open.spotify.com",
    "huggingface.co",
    "music.apple.com",
    "soundcloud.com",
    "i.redd.it",
    "imgur.com",
    "flickr.com",
    "pastebin.com",
    "genshindle.com"
]

async def update_cache():
    try:
        print("Downloading latest PhishTank data...")
        async with aiohttp.ClientSession(headers=PHISHTANK_USER_AGENT) as session:
            async with session.get(PHISHTANK_URL, timeout=15) as r:
                r.raise_for_status()
                data = await r.json()
        with open(PHISHTANK_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
        print("PhishTank data updated.")
    except Exception as e:
        print(f"Error updating cache: {e}")

def check_phishtank(url: str) -> bool:
    """
    Checks if the given URL is listed in PhishTank's verified phishing database.
    No API key required. Uses the public hourly-updated JSON feed.
    """
    try:
        with open(PHISHTANK_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return any(entry["url"].strip("/") == url.strip("/") for entry in data)
    except Exception as e:
        print(f"Error reading cache: {e}")
        return False
    
async def check_url_google_safe_browsing(api_key, url):
    # Check if the URL is in the list of safe URLs
    if any(safe_url in url for safe_url in SAFE_URLS):
        return False

    endpoint = f"https://safebrowsing.googleapis.com/v4/threatMatches:find?key={api_key}"
    body = {
        "client": {"clientId": "ModeratorBot", "clientVersion": "1.0"},
        "threatInfo": {
            "threatTypes": ["MALWARE", "SOCIAL_ENGINEERING", "UNWANTED_SOFTWARE", "POTENTIALLY_HARMFUL_APPLICATION"],
            "platformTypes": ["ANY_PLATFORM"],
            "threatEntryTypes": ["URL"],
            "threatEntries": [{"url": url}]
        }
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(endpoint, json=body) as response:
            data = await response.json()
    return bool(data.get("matches"))

async def _url_is_scam(url_lower: str, guild_id: int) -> bool:
    """Run *all* DB / Safe-list / PhishTank / GSB checks for a single URL."""
    # DB substring-match against scam_urls
    match_row, _ = await execute_query(
        """SELECT full_url FROM scam_urls
           WHERE (guild_id=%s OR global_verified=TRUE)
           AND LOCATE(full_url, %s) > 0""",
        (guild_id, url_lower), fetch_one=True
    )
    if match_row:
        return True

    # skip completely safe domains
    if any(safe in url_lower for safe in SAFE_URLS):
        return False

    # exact known URL
    already_known, _ = await execute_query(
        "SELECT 1 FROM scam_urls WHERE (guild_id=%s OR global_verified=TRUE) AND full_url=%s",
        (guild_id, url_lower), fetch_one=True
    )
    if already_known:
        return True

    # PhishTank / Google Safe Browsing
    if check_phishtank(url_lower) or await check_url_google_safe_browsing(GOOGLE_API_KEY, url_lower):
        await execute_query(
            """INSERT INTO scam_urls (guild_id, full_url, added_by, global_verified)
               VALUES (%s, %s, %s, TRUE)
               ON DUPLICATE KEY UPDATE global_verified = TRUE""",
            (guild_id, url_lower, 0)
        )
        return True
    return False

async def is_scam_message(message: str, guild_id: int) -> tuple[bool, str | None, str | None]:
    content_l = message.lower()
    normalized_message = normalize_text(content_l)

    patterns, _ = await execute_query(
        "SELECT pattern FROM scam_messages WHERE guild_id=%s OR global_verified=TRUE",
        (guild_id,), fetch_all=True
    )
    matched_pattern = next((p[0] for p in patterns if p[0].lower() in normalized_message), None)
    if matched_pattern:
        return True, matched_pattern, None


    normalized_urls = extract_urls(message, 
                                   normalize=True)

    check_links   = await get_settings(guild_id, CHECK_LINKS_SETTING)

    if check_links:
        for url in normalized_urls:
            if await _url_is_scam(url.lower(), guild_id):
                return True, None, url

            expanded = await unshorten_url(url)
            if expanded != url and await _url_is_scam(expanded.lower(), guild_id):
                return True, None, expanded

    return False, None, None

class ScamDetectionCog(commands.Cog):
    """Detect scam messages / URLs and let mods manage patterns + settings."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.scam_schedule.start()
        self.free_queue = WorkerQueue(max_workers=1)
        self.accelerated_queue = WorkerQueue(max_workers=3)

    scam_group = app_commands.Group(
        name="scam",
        description="Scam-detection configuration and pattern management.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_messages=True),
    )

    @scam_group.command(name="exclude_channel_add", description="Exclude a channel from scam detection.")
    @app_commands.describe(channel="The channel to exclude")
    async def exclude_channel_add(self, interaction: Interaction, channel: discord.TextChannel):
        gid = interaction.guild.id
        if channel.id in await get_settings(gid, EXCLUDE_CHANNELS_SETTING):
            await interaction.response.send_message(
                f"Channel {channel.mention} is already excluded from scam detection.", ephemeral=True
            )
            return
        current_excluded = await get_settings(gid, EXCLUDE_CHANNELS_SETTING) or []
        current_excluded.append(channel.id)
        await update_settings(gid, EXCLUDE_CHANNELS_SETTING, current_excluded)
        await interaction.response.send_message(
            f"Channel {channel.mention} has been excluded from scam detection.", ephemeral=True
        )
    
    @scam_group.command(name="exclude_channel_remove", description="Remove a channel from the exclusion list.")
    @app_commands.describe(channel="The channel to remove from exclusion")
    async def exclude_channel_remove(self, interaction: Interaction, channel: discord.TextChannel):
        gid = interaction.guild.id
        current_excluded = await get_settings(gid, EXCLUDE_CHANNELS_SETTING) or []
        if channel.id not in current_excluded:
            await interaction.response.send_message(
                f"Channel {channel.mention} is not excluded from scam detection.", ephemeral=True
            )
            return
        current_excluded.remove(channel.id)
        await update_settings(gid, EXCLUDE_CHANNELS_SETTING, current_excluded)
        await interaction.response.send_message(
            f"Channel {channel.mention} has been removed from the exclusion list.", ephemeral=True
        )
    
    @scam_group.command(name="exclude_channel_list", description="List all excluded channels.")
    async def exclude_channel_list(self, interaction: Interaction):
        gid = interaction.guild.id
        excluded_channels = await get_settings(gid, EXCLUDE_CHANNELS_SETTING) or []
        if not excluded_channels:
            await interaction.response.send_message("No channels are currently excluded from scam detection.", ephemeral=True)
            return
        channels = [interaction.guild.get_channel(cid) for cid in excluded_channels if interaction.guild.get_channel(cid)]
        if not channels:
            await interaction.response.send_message("No valid excluded channels found.", ephemeral=True)
            return
        channel_mentions = ", ".join(channel.mention for channel in channels)
        await interaction.response.send_message(
            f"Excluded channels: {channel_mentions}", ephemeral=True
        )

    @scam_group.command(name="check_links", description="Toggle or view link checking.")
    @app_commands.describe(action="enable | disable | status")
    @app_commands.choices(
        action=[
            app_commands.Choice(name="enable",  value="enable"),
            app_commands.Choice(name="disable", value="disable"),
            app_commands.Choice(name="status",  value="status"),
        ]
    )
    async def setting_check_links(self, interaction: Interaction,
                                  action: app_commands.Choice[str]):
        """Toggle or view link checking setting."""
        gid = interaction.guild.id
        if action.value == "status":
            flag = await get_settings(gid, CHECK_LINKS_SETTING)
            await interaction.response.send_message(
                f"Link checking is **{'enabled' if flag else 'disabled'}**.", ephemeral=True
            )
            return
        await update_settings(gid, CHECK_LINKS_SETTING, action.value == "enable")
        await interaction.response.send_message(
            f"Link checking **{action.value}d**.", ephemeral=True
        )

    @scam_group.command(name="add_action", description="Add an action to the scam punishment list.")
    @app_commands.describe(
        action="Action to perform",
        duration="Only required for timeout (e.g. 10m, 1h, 3d)"
    )
    @app_commands.choices(action=action_choices())
    async def scam_add_action(
        self,
        interaction: Interaction,
        action: str,
        duration: str = None,
        role: discord.Role = None,
        reason: str = None,
    ):
        await interaction.response.defer(ephemeral=True)
        
        gid = interaction.guild.id
        action_str = await validate_action(
            interaction=interaction,
            action=action,
            duration=duration,
            role=role,
            valid_actions=VALID_ACTION_VALUES,
            param=reason,
        )
        if action_str is None:
            return

        msg = await manager.add_action(gid, action_str)
        await interaction.followup.send(msg, ephemeral=True)

    @scam_group.command(name="remove_action", description="Remove an action from the scam punishment list.")
    @app_commands.describe(action="Exact action string to remove (e.g. timeout:1d, delete)")
    @app_commands.autocomplete(action=manager.autocomplete)
    async def scam_remove_action(self, interaction: Interaction, action: str):
        gid = interaction.guild.id
        msg = await manager.remove_action(gid, action)
        await interaction.response.send_message(msg, ephemeral=True)

    @scam_group.command(name="view", description="View current scam settings.")
    async def settings_view(self, interaction: Interaction):
        gid = interaction.guild.id
        action_setting = await manager.view_actions(gid)

        if not action_setting:
            actions_formatted = "*No actions set.*"
        else:
            actions_formatted = "\n".join(f"  - `{a}`" for a in action_setting)

        await interaction.response.send_message(
            f"**Scam Settings:**\n"
            f"- Scam actions:\n{actions_formatted}",
            ephemeral=True
        )

    async def add_to_queue(self, coro, guild_id: int):
        """
        Add a task to the appropriate queue.
        accelerated=True means higher priority
        """
        accelerated = await get_settings(guild_id, "scam-accelerated")
        queue = self.accelerated_queue if accelerated else self.free_queue
        await queue.add_task(coro)

    async def handle_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        gid = message.guild.id
        content_l = message.content.lower()

        # load settings
        action_flag = await get_settings(gid, ACTION_SETTING)
        exclude_channels = await get_settings(gid, EXCLUDE_CHANNELS_SETTING) or []

        # Check if the channel is excluded
        if message.channel.id in exclude_channels:
            return
        async def scan_task():
            # Run the scam detection
            is_scam, matched_pattern, matched_url = await is_scam_message(content_l, gid)

            if not is_scam:
                return
            
            # Log the user and message
            await execute_query(
                """INSERT INTO scam_users
                    (user_id,guild_id,matched_message_id,matched_pattern,matched_url)
                VALUES (%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE first_detected=first_detected""",
                (message.author.id, gid, message.id, matched_pattern, matched_url),
            )

            if action_flag:
                try:
                    await strike.perform_disciplinary_action(
                        user=message.author,
                        bot=self.bot,
                        action_string=action_flag,
                        reason="Scam message detected",
                        source="scam",
                        message=message,
                    )
                except Exception:
                    pass

            try:
                embed = discord.Embed(
                    title="Scam Message Detected",
                    description=f"{message.author.mention}, your message was flagged as scam.",
                    color=discord.Color.red()
                )
                embed.set_thumbnail(url=message.author.display_avatar.url)
                await mod_logging.log_to_channel(
                    embed=embed,
                    channel_id=message.channel.id,
                    bot=self.bot
                )
            except Exception:
                pass
        await self.add_to_queue(scan_task(), guild_id=gid)

    @tasks.loop(hours=6)
    async def scam_schedule(self):
        try:
            print("[PhishTank] Auto-refresh started...")
            await update_cache()
            print("[PhishTank] Cache refreshed successfully.")

            print("[URLExtract] Updating TLD list...")
            update_tld_list()
            print("[URLExtract] TLD list updated.")

        except Exception as e:
            print(f"[scam_schedule] Error during scheduled refresh: {e}")

    async def cog_load(self):
        await self.free_queue.start()
        await self.accelerated_queue.start()

    async def cog_unload(self):
        self.scam_schedule.cancel()
        await self.free_queue.stop()
        await self.accelerated_queue.stop()

async def setup(bot: commands.Bot):
    await bot.add_cog(ScamDetectionCog(bot))
