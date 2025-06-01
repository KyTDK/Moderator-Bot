from discord.ext import commands
from discord import app_commands, Interaction, Message
from modules.utils.mysql import execute_query, get_settings, update_settings
from modules.moderation import strike
import re
from modules.utils.strike import validate_action_with_duration
from transformers import pipeline

DELETE_SETTING = "delete-scam-messages"
ACTION_SETTING = "scam-detection-action"
AI_DECTION_SETTING = "ai-scam-detection"

URL_RE = re.compile(r"(https?://|www\.)\S+")

classifier = pipeline("text-classification", model="mrm8488/bert-tiny-finetuned-sms-spam-detection")

def is_scam_message(message: str) -> bool:
    result = classifier(message)[0]
    return result['label'].lower() == 'spam' and result['score'] > 0.8

class ScamDetectionCog(commands.Cog):
    """Detect scam messages / URLs and let mods manage patterns + settings."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    scam_group = app_commands.Group(
        name="scam",
        description="Scam-detection configuration and pattern management.",
        guild_only=True,
    )

    settings_group = app_commands.Group(
        name="settings",
        description="View / change scam-detection settings.",
        parent=scam_group,
    )

    @settings_group.command(name="delete", description="Toggle or view auto-delete.")
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

    @settings_group.command(name="ai_detection", description="Toggle or view AI scam detection.")
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

    @settings_group.command(name="action", description="Set the scam punishment action.")
    @app_commands.describe(
        action="Action: strike, kick, ban, timeout",
        duration="Only required for timeout (e.g. 10m, 1h, 3d)"
    )
    async def setting_action(
        self,
        interaction: Interaction,
        action: str = None,
        duration: str = None
    ):
        gid = interaction.guild.id

        action_str = await validate_action_with_duration(
            interaction=interaction,
            action=action,
            duration=duration,
            valid_actions=["strike", "kick", "ban", "timeout"]
        )
        if action_str is None:
            return

        await update_settings(gid, ACTION_SETTING, action_str)
        await interaction.response.send_message(
            f"Scam action set to `{action_str}`.", ephemeral=True
        )

    @settings_group.command(name="view", description="View current scam settings.")
    async def settings_view(self, interaction: Interaction):
        gid = interaction.guild.id
        delete_setting = await get_settings(gid, DELETE_SETTING)
        action_setting = await get_settings(gid, ACTION_SETTING)
        ai_scam_detection = await get_settings(gid, AI_DECTION_SETTING)

        await interaction.response.send_message(
            f"**Scam Settings:**\n"
            f"- Delete scam messages: `{delete_setting}`\n"
            f"- AI scam detection: `{ai_scam_detection}`\n"
            f"- Scam action: `{action_setting}`",
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
        listing = "\n".join(f"- {url} ({'✅' if v else '❌'})" for url, v in rows)
        await interaction.response.send_message(f"**Scam URLs:**\n{listing}", ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: Message):
        if message.author.bot or not message.guild:
            return

        gid = message.guild.id
        content_l = message.content.lower()

        # load settings
        delete_flag = await get_settings(gid, DELETE_SETTING)
        action_flag = await get_settings(gid, ACTION_SETTING)
        ai_detection_flag = await get_settings(gid, AI_DECTION_SETTING)

        # fetch patterns / URLs
        patterns, _ = await execute_query(
            "SELECT pattern FROM scam_messages WHERE guild_id=%s OR global_verified=TRUE",
            (gid,), fetch_all=True,
        )
        urls, _ = await execute_query(
            "SELECT full_url FROM scam_urls WHERE guild_id=%s OR global_verified=TRUE",
            (gid,), fetch_all=True,
        )

        matched_pattern = next((p[0] for p in patterns if p[0].lower() in content_l), None)
        matched_url = next((u[0] for u in urls if u[0].lower() in content_l), None)

        # If no pattern or URL matched, check via AI (if enabled)
        if not (matched_pattern or matched_url):
            if not ai_detection_flag:
                return
            if not is_scam_message(content_l):
                return
            matched_pattern = message.content  # Use the full message as the matched pattern
            matched_url = None

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
                )
            except Exception:
                pass

        try:
            await message.channel.send(
                f"{message.author.mention}, your message was flagged as scam and has been removed."
            )
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(ScamDetectionCog(bot))