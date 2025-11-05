import json
import os
import asyncio
import discord
from discord.ext import commands
from discord import app_commands, Interaction
from dotenv import load_dotenv
from modules.utils import mod_logging
from modules.utils.mysql import execute_query, get_settings, update_settings, is_accelerated
from modules.moderation import strike
from modules.utils.action_manager import ActionListManager
from modules.utils.action_command_helpers import (
    process_add_action,
    process_remove_action,
)
from modules.utils.actions import action_choices, VALID_ACTION_VALUES
from modules.utils.text import normalize_text
import aiohttp
from discord.ext import tasks
from modules.utils.url_utils import extract_urls, unshorten_url, update_tld_list
from modules.worker_queue import WorkerQueue
from modules.worker_queue_alerts import SingularTaskReporter
from modules.core.moderator_bot import ModeratorBot
from modules.i18n.strings import locale_string
from modules.utils.discord_utils import require_accelerated

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

    check_links = bool(await get_settings(guild_id, CHECK_LINKS_SETTING))

    if check_links:
        try:
            accelerated = await is_accelerated(guild_id=guild_id)
        except Exception:  # pragma: no cover - defensive
            accelerated = False
        if not accelerated:
            check_links = False

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

    def __init__(self, bot: ModeratorBot):
        self.bot = bot
        # Start background tasks after cog is fully loaded to avoid blocking startup
        self._singular_task_reporter = SingularTaskReporter(bot)
        self.accelerated_queue = WorkerQueue(
            max_workers=3,
            name="scam_detection_accelerated",
            singular_task_reporter=self._singular_task_reporter,
            developer_log_bot=bot,
            developer_log_context="scam_detection.accelerated_queue",
        )

    scam_group = app_commands.Group(
        name="scam",
        description=locale_string("cogs.scam_detection.meta.group_description"),
        guild_only=True,
        default_permissions=discord.Permissions(manage_messages=True),
    )

    @scam_group.command(
        name="exclude_channel_add",
        description=locale_string("cogs.scam_detection.meta.exclude_channel_add.description"),
    )
    @app_commands.describe(
        channel=locale_string("cogs.scam_detection.meta.exclude_channel_add.channel")
    )
    async def exclude_channel_add(self, interaction: Interaction, channel: discord.TextChannel):
        gid = interaction.guild.id
        texts = self.bot.translate("cogs.scam_detection.exclude",
                                    guild_id=gid)
        current_excluded = await get_settings(gid, EXCLUDE_CHANNELS_SETTING) or []
        if channel.id in current_excluded:
            await interaction.response.send_message(
                texts["already"].format(channel=channel.mention),
                ephemeral=True,
            )
            return
        current_excluded.append(channel.id)
        await update_settings(gid, EXCLUDE_CHANNELS_SETTING, current_excluded)
        await interaction.response.send_message(
            texts["added"].format(channel=channel.mention),
            ephemeral=True,
        )

    @scam_group.command(
        name="exclude_channel_remove",
        description=locale_string("cogs.scam_detection.meta.exclude_channel_remove.description"),
    )
    @app_commands.describe(
        channel=locale_string("cogs.scam_detection.meta.exclude_channel_remove.channel")
    )
    async def exclude_channel_remove(self, interaction: Interaction, channel: discord.TextChannel):
        gid = interaction.guild.id
        texts = self.bot.translate("cogs.scam_detection.exclude",
                                    guild_id=gid)
        current_excluded = await get_settings(gid, EXCLUDE_CHANNELS_SETTING) or []
        if channel.id not in current_excluded:
            await interaction.response.send_message(
                texts["not_excluded"].format(channel=channel.mention),
                ephemeral=True,
            )
            return
        current_excluded.remove(channel.id)
        await update_settings(gid, EXCLUDE_CHANNELS_SETTING, current_excluded)
        await interaction.response.send_message(
            texts["removed"].format(channel=channel.mention),
            ephemeral=True,
        )

    @scam_group.command(
        name="exclude_channel_list",
        description=locale_string("cogs.scam_detection.meta.exclude_channel_list.description"),
    )
    async def exclude_channel_list(self, interaction: Interaction):
        gid = interaction.guild.id
        texts = self.bot.translate("cogs.scam_detection.exclude",
                                    guild_id=gid)
        excluded_channels = await get_settings(gid, EXCLUDE_CHANNELS_SETTING) or []
        if not excluded_channels:
            await interaction.response.send_message(texts["list_empty"], ephemeral=True)
            return
        channels = [interaction.guild.get_channel(cid) for cid in excluded_channels if interaction.guild.get_channel(cid)]
        if not channels:
            await interaction.response.send_message(texts["invalid"], ephemeral=True)
            return
        channel_mentions = ", ".join(channel.mention for channel in channels)
        await interaction.response.send_message(
            texts["list"].format(channels=channel_mentions),
            ephemeral=True,
        )
    @scam_group.command(
        name="check_links",
        description=locale_string("cogs.scam_detection.meta.check_links.description"),
    )
    @app_commands.describe(
        action=locale_string("cogs.scam_detection.meta.check_links.action_description")
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(
                name=locale_string("cogs.scam_detection.meta.check_links.choices.enable"),
                value="enable",
            ),
            app_commands.Choice(
                name=locale_string("cogs.scam_detection.meta.check_links.choices.disable"),
                value="disable",
            ),
            app_commands.Choice(
                name=locale_string("cogs.scam_detection.meta.check_links.choices.status"),
                value="status",
            ),
        ]
    )
    async def setting_check_links(self, interaction: Interaction,
                                  action: app_commands.Choice[str]):
        """Toggle or view link checking setting."""
        gid = interaction.guild.id
        texts = self.bot.translate("cogs.scam_detection.check_links",
                                    guild_id=gid)
        if not await require_accelerated(interaction):
            return
        if action.value == "status":
            flag = await get_settings(gid, CHECK_LINKS_SETTING)
            await interaction.response.send_message(
                texts["status"].format(
                    state=texts["state"]["enabled" if flag else "disabled"],
                ),
                ephemeral=True,
            )
            return
        enabled = action.value == "enable"
        await update_settings(gid, CHECK_LINKS_SETTING, enabled)
        await interaction.response.send_message(
            texts["updated"].format(
                state=texts["state"]["enabled" if enabled else "disabled"],
            ),
            ephemeral=True,
        )

    @scam_group.command(
        name="add_action",
        description=locale_string("cogs.scam_detection.meta.add_action.description"),
    )
    @app_commands.describe(
        action=locale_string("cogs.scam_detection.meta.add_action.action"),
        duration=locale_string("cogs.scam_detection.meta.add_action.duration"),
        channel=locale_string(
            "cogs.scam_detection.meta.add_action.channel",
            default="Channel to broadcast messages to.",
        ),
    )
    @app_commands.choices(action=action_choices())
    async def scam_add_action(
        self,
        interaction: Interaction,
        action: str,
        duration: str = None,
        role: discord.Role = None,
        channel: discord.TextChannel = None,
        reason: str = None,
    ):
        gid = interaction.guild.id

        await process_add_action(
            interaction,
            manager=manager,
            translator=self.bot.translate,
            validate_kwargs={
                "interaction": interaction,
                "action": action,
                "duration": duration,
                "role": role,
                "channel": channel,
                "valid_actions": VALID_ACTION_VALUES,
                "param": reason,
                "translator": self.bot.translate,
            },
        )

    @scam_group.command(
        name="remove_action",
        description=locale_string("cogs.scam_detection.meta.remove_action.description"),
    )
    @app_commands.describe(
        action=locale_string("cogs.scam_detection.meta.remove_action.action")
    )
    @app_commands.autocomplete(action=manager.autocomplete)
    async def scam_remove_action(self, interaction: Interaction, action: str):
        gid = interaction.guild.id

        await process_remove_action(
            interaction,
            manager=manager,
            translator=self.bot.translate,
            action=action,
        )

    @scam_group.command(
        name="view",
        description=locale_string("cogs.scam_detection.meta.view.description"),
    )
    async def settings_view(self, interaction: Interaction):
        gid = interaction.guild.id
        action_setting = await manager.view_actions(gid)
        texts = self.bot.translate("cogs.scam_detection.settings",
                                   guild_id=gid)

        if not action_setting:
            actions_formatted = texts["none"]
        else:
            actions_formatted = "\n".join(f"  - `{a}`" for a in action_setting)

        message = "\n".join(
            [
                texts["heading"],
                texts["actions"].format(actions=actions_formatted),
            ]
        )
        await interaction.response.send_message(message, ephemeral=True)

    async def add_to_queue(self, coro, guild_id: int):
        """Enqueue scam detection work for premium-enabled guilds only."""
        accelerated = await get_settings(guild_id, "scam-accelerated")
        if not accelerated:
            return
        await self.accelerated_queue.add_task(coro)

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

            if action_flag:
                try:
                    await strike.perform_disciplinary_action(
                        user=message.author,
                        bot=self.bot,
                        action_string=action_flag,
                        reason=self.bot.translate("cogs.scam_detection.detection.reason",
                                                    guild_id=gid),
                        source="scam",
                        message=message,
                    )
                except Exception:
                    pass

            detection_texts = self.bot.translate("cogs.scam_detection.detection",
                                                 guild_id=gid)
            try:
                embed = discord.Embed(
                    title=detection_texts["title"],
                    description=detection_texts["description"].format(mention=message.author.mention),
                    color=discord.Color.red(),
                )
                embed.set_thumbnail(url=message.author.display_avatar.url)
                await mod_logging.log_to_channel(
                    embed=embed,
                    channel_id=message.channel.id,
                    bot=self.bot,
                )
            except Exception:
                pass
        await self.add_to_queue(scan_task(), guild_id=gid)

    @tasks.loop(hours=6)
    async def scam_schedule(self):
        try:
            print("[PhishTank] Auto-refresh started...")
            # Run refresh with an upper bound to avoid long blocking
            try:
                await asyncio.wait_for(update_cache(), timeout=30)
            except asyncio.TimeoutError:
                print("[PhishTank] Refresh timed out; will retry later.")
            print("[PhishTank] Cache refreshed successfully.")

            print("[URLExtract] Updating TLD list...")
            update_tld_list()
            print("[URLExtract] TLD list updated.")

        except Exception as e:
            print(f"[scam_schedule] Error during scheduled refresh: {e}")

    @scam_schedule.before_loop
    async def before_scam_schedule(self):
        # Ensure the bot is fully ready before first run
        await self.bot.wait_until_ready()

    async def cog_load(self):
        await self.accelerated_queue.start()
        # Start the scheduled task only after the cog is loaded
        self.scam_schedule.start()

    async def cog_unload(self):
        self.scam_schedule.cancel()
        await self.accelerated_queue.stop()

async def setup(bot: commands.Bot):
    await bot.add_cog(ScamDetectionCog(bot))
