import json
import os
import discord
from discord.ext import commands
from discord import app_commands, Interaction
from dotenv import load_dotenv
from modules.utils.mysql import execute_query, get_settings, update_settings
from modules.moderation import strike
from urlextract import URLExtract
from modules.utils.strike import validate_action
from modules.utils.actions import action_choices, VALID_ACTION_VALUES
from transformers import pipeline
from cogs.banned_words import normalize_text
import aiohttp
from discord.ext import tasks
    
load_dotenv()
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')

DELETE_SETTING = "delete-scam-messages"
ACTION_SETTING = "scam-detection-action"
AI_DECTION_SETTING = "ai-scam-detection"
EXCLUDE_CHANNELS_SETTING = "exclude-scam-channels"
CHECK_LINKS_SETTING = "check-links"

PHISHTANK_URL = "http://data.phishtank.com/data/online-valid.json"
PHISHTANK_CACHE_FILE = "phishtank_cache.json"
PHISHTANK_USER_AGENT = {"User-Agent": "ModeratorBot/1.0"}

url_extractor = URLExtract()

classifier = pipeline("text-classification", model="mshenoda/roberta-spam")

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
    "music.apple.com"
]

INTERMEDIARY_DOMAINS = ["antiphishing.biz",]

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

async def unshorten_url(url: str) -> str:
    import httpx
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with httpx.AsyncClient(follow_redirects=True, headers=headers, timeout=10.0, verify=False) as client:
            resp = await client.get(url)
            final_url = str(resp.url)
            if any(domain in final_url for domain in INTERMEDIARY_DOMAINS):
                print(f"[Redirect Skipped] Landed on intermediary: {final_url}")
                return url
            print(f"[URL Unshortened] {url} -> {final_url}")
            return final_url
    except Exception as e:
        print(f"[unshorten_url] Failed to unshorten {url}: {e}")
        return url

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
        print(f"URL {url} is in the safe list, skipping Google Safe Browsing check.")
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
    print(f"Checked URL: {url}, Response: {data}")
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
        print(f"[ScamMatch] Known URL pattern: {match_row[0]}")
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

    found_urls = url_extractor.find_urls(message)
    normalized_urls = [
        u if u.startswith(("http://", "https://")) else f"http://{u}"
        for u in found_urls
    ]

    check_links   = await get_settings(guild_id, CHECK_LINKS_SETTING)
    ai_detection  = await get_settings(guild_id, AI_DECTION_SETTING)

    if check_links:
        for url in normalized_urls:
            if await _url_is_scam(url.lower(), guild_id):
                return True, None, url

            expanded = await unshorten_url(url)
            if expanded != url and await _url_is_scam(expanded.lower(), guild_id):
                return True, None, expanded

    if ai_detection and len(normalized_message.split()) >= 5:
        result = classifier(normalized_message)[0]
        if result['label'] == 'LABEL_1' and result['score'] > 0.95:
            return True, message, None

    return False, None, None

class ScamDetectionCog(commands.Cog):
    """Detect scam messages / URLs and let mods manage patterns + settings."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.scam_schedule.start()

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

    @scam_group.command(name="delete", description="Toggle or view auto-delete.")
    @app_commands.describe(action="enable | disable | status")
    @app_commands.choices(
        action=[
            app_commands.Choice(name="enable",  value="enable"),
            app_commands.Choice(name="disable", value="disable"),
            app_commands.Choice(name="status",  value="status"),
        ]
    )
    async def setting_delete(self, interaction: Interaction,
                             action: app_commands.Choice[str]):
        gid = interaction.guild.id
        if action.value == "status":
            flag = await get_settings(gid, DELETE_SETTING)
            await interaction.response.send_message(
                f"Auto-delete is **{'enabled' if flag else 'disabled'}**.", ephemeral=True
            )
            return
        await update_settings(gid, DELETE_SETTING, action.value == "enable")
        await interaction.response.send_message(
            f"Auto-delete **{action.value}d**.", ephemeral=True
        )

    @scam_group.command(name="ai_detection", description="Toggle or view AI scam detection.")
    @app_commands.describe(action="enable | disable | status")
    @app_commands.choices(
        action=[
            app_commands.Choice(name="enable",  value="enable"),
            app_commands.Choice(name="disable", value="disable"),
            app_commands.Choice(name="status",  value="status"),
        ]
    )
    async def setting_ai_detection(self, interaction: Interaction,
                                   action: app_commands.Choice[str]):
        gid = interaction.guild.id
        if action.value == "status":
            flag = await get_settings(gid, AI_DECTION_SETTING)
            await interaction.response.send_message(
                f"AI scam detection is **{'enabled' if flag else 'disabled'}**.", ephemeral=True
            )
            return
        await update_settings(gid, AI_DECTION_SETTING, action.value == "enable")
        await interaction.response.send_message(
            f"AI scam detection **{action.value}d**.", ephemeral=True
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
        role: discord.Role = None
    ):
        await interaction.response.defer(ephemeral=True)
        
        gid = interaction.guild.id
        action_str = await validate_action(
            interaction=interaction,
            action=action,
            duration=duration,
            role=role,
            valid_actions=VALID_ACTION_VALUES,
        )
        if action_str is None:
            return

        current = await get_settings(gid, ACTION_SETTING) or []
        if not isinstance(current, list):
            current = [current] if current else []

        if action_str in current:
            await interaction.response.send_message(f"`{action_str}` is already in the action list.", ephemeral=True)
            return

        current.append(action_str)
        await update_settings(gid, ACTION_SETTING, current)
        await interaction.response.send_message(f"Added `{action_str}` to scam actions.", ephemeral=True)

    @scam_group.command(name="remove_action", description="Remove an action from the scam punishment list.")
    @app_commands.describe(action="Exact action string to remove (e.g. timeout:1d, delete)")
    async def scam_remove_action(self, interaction: Interaction, action: str):
        gid = interaction.guild.id
        current = await get_settings(gid, ACTION_SETTING) or []

        if action not in current:
            await interaction.response.send_message(f"`{action}` is not in the action list.", ephemeral=True)
            return

        current.remove(action)
        await update_settings(gid, ACTION_SETTING, current)
        await interaction.response.send_message(f"Removed `{action}` from scam actions.", ephemeral=True)

    @scam_group.command(name="view", description="View current scam settings.")
    async def settings_view(self, interaction: Interaction):
        gid = interaction.guild.id
        delete_setting = await get_settings(gid, DELETE_SETTING)
        action_setting = await get_settings(gid, ACTION_SETTING)
        ai_scam_detection = await get_settings(gid, AI_DECTION_SETTING)

        if not action_setting:
            actions_formatted = "*No actions set.*"
        else:
            if not isinstance(action_setting, list):
                action_setting = [action_setting]
            actions_formatted = "\n".join(f"  - `{a}`" for a in action_setting)

        await interaction.response.send_message(
            f"**Scam Settings:**\n"
            f"- Delete scam messages: `{delete_setting}`\n"
            f"- AI scam detection: `{ai_scam_detection}`\n"
            f"- Scam actions:\n{actions_formatted}",
            ephemeral=True
        )

    @scam_group.command(name="add_message", description="Add a scam message pattern.")
    async def add_message(self, interaction: Interaction, pattern: str):
        gid, uid = interaction.guild.id, interaction.user.id
        await execute_query(
            """INSERT INTO scam_messages (guild_id, pattern, added_by)
               VALUES (%s,%s,%s)
               ON DUPLICATE KEY UPDATE added_at=CURRENT_TIMESTAMP""",
            (gid, pattern, uid),
        )
        await interaction.response.send_message(f"Pattern added: `{pattern}`", ephemeral=True)

    @scam_group.command(name="add_url", description="Add a scam URL (full or substring).")
    async def add_url(self, interaction: Interaction, url: str):
        gid, uid = interaction.guild.id, interaction.user.id
        await execute_query(
            """INSERT INTO scam_urls (guild_id, full_url, added_by)
               VALUES (%s,%s,%s)
               ON DUPLICATE KEY UPDATE added_at=CURRENT_TIMESTAMP""",
            (gid, url.lower(), uid),
        )
        await interaction.response.send_message(f"URL added: `{url}`", ephemeral=True)

    @scam_group.command(name="remove_message", description="Remove a scam message pattern.")
    async def remove_message(self, interaction: Interaction, pattern: str):
        gid = interaction.guild.id
        result, affected = await execute_query(
            "DELETE FROM scam_messages WHERE guild_id = %s AND pattern = %s",
            (gid, pattern)
        )
        if affected > 0:
            await interaction.response.send_message(f"Removed pattern: `{pattern}`", ephemeral=True)
        else:
            await interaction.response.send_message(f"No such pattern found: `{pattern}`", ephemeral=True)

    @scam_group.command(name="remove_url", description="Remove a scam URL.")
    async def remove_url(self, interaction: Interaction, url: str):
        gid = interaction.guild.id
        result, affected = await execute_query(
            "DELETE FROM scam_urls WHERE guild_id = %s AND full_url = %s",
            (gid, url.lower())
        )
        if affected > 0:
            await interaction.response.send_message(f"Removed URL: `{url}`", ephemeral=True)
        else:
            await interaction.response.send_message(f"No such URL found: `{url}`", ephemeral=True)

    @scam_group.command(name="list_patterns", description="Show this guild’s scam patterns.")
    async def list_patterns(self, interaction: Interaction):
        gid = interaction.guild.id
        rows, _ = await execute_query(
            "SELECT pattern, global_verified FROM scam_messages WHERE guild_id=%s",
            (gid,), fetch_all=True,
        )
        if not rows:
            await interaction.response.send_message("No patterns recorded.", ephemeral=True)
            return
        listing = "\n".join(f"- {p} ({'✅' if v else '❌'})" for p, v in rows)
        await interaction.response.send_message(f"**Patterns:**\n{listing}", ephemeral=True)

    @scam_group.command(name="list_urls", description="Show this guild’s scam URLs.")
    async def list_urls(self, interaction: Interaction):
        gid = interaction.guild.id
        rows, _ = await execute_query(
            "SELECT full_url, global_verified FROM scam_urls WHERE guild_id=%s",
            (gid,), fetch_all=True,
        )
        if not rows:
            await interaction.response.send_message("No scam URLs recorded.", ephemeral=True)
            return
        listing = "\n".join(f"- <{url}> ({'✅' if v else '❌'})" for url, v in rows)
        await interaction.response.send_message(f"**Scam URLs:**\n{listing}", ephemeral=True)

    async def handle_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        gid = message.guild.id
        content_l = message.content.lower()

        # load settings
        delete_flag = await get_settings(gid, DELETE_SETTING)
        action_flag = await get_settings(gid, ACTION_SETTING)
        exclude_channels = await get_settings(gid, EXCLUDE_CHANNELS_SETTING) or []

        # Check if the channel is excluded
        if message.channel.id in exclude_channels:
            return

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

        if delete_flag:
            try:
                await message.delete()
            except Exception:
                pass

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
            await message.channel.send(
                f"{message.author.mention}, your message was flagged as scam and has been removed."
            )
        except Exception:
            pass

    @tasks.loop(hours=6)
    async def scam_schedule(self):
        try:
            print("[PhishTank] Auto-refresh started...")
            await update_cache()
            print("[PhishTank] Cache refreshed successfully.")

            print("[URLExtract] Updating TLD list...")
            url_extractor.update()
            print("[URLExtract] TLD list updated.")

        except Exception as e:
            print(f"[scam_schedule] Error during scheduled refresh: {e}")

    def cog_unload(self):
        self.scam_schedule.cancel()

async def setup(bot: commands.Bot):
    await bot.add_cog(ScamDetectionCog(bot))