import discord
from discord import app_commands, Interaction
from discord.ext import commands
from typing import Optional

from modules.utils import mysql
from modules.utils.mod_logging import log_to_channel
from modules.utils.discord_utils import safe_get_member, safe_get_user, ensure_member_with_presence
from modules.antibot.scoring import evaluate_member
from modules.antibot.embeds import build_inspection_embed, build_join_embed


class AntiBotCog(commands.Cog):
    """Anti-bot utilities: inspect users and (optionally) auto-act on joins."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------- Slash commands ----------
    antibot = app_commands.Group(
        name="antibot",
        description="Anti-bot tools and inspection",
        guild_only=True,
        default_permissions=discord.Permissions(administrator=True),
    )

    @antibot.command(name="inspect", description="Inspect a user's signals and compute a trust score")
    @app_commands.describe(user="Select a user to inspect (or provide ID)", user_id="Optional user ID if not selectable")
    async def inspect(self, interaction: Interaction, user: Optional[discord.User] = None, user_id: Optional[str] = None):
        await interaction.response.defer(ephemeral=True)

        # Resolve the member within this guild
        target_member: Optional[discord.Member] = None
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("This command can only be used in a server.", ephemeral=True)
            return

        # Prefer an explicit chooser, then ID fallback
        if user is not None:
            target_member = await safe_get_member(guild, user.id, force_fetch=True)
        elif user_id and user_id.isdigit():
            target_member = await safe_get_member(guild, int(user_id), force_fetch=True)

        if target_member is None:
            await interaction.followup.send("Could not resolve that user as a member of this server.", ephemeral=True)
            return

        # Try to enhance the Member with presence/activities for better signals
        try:
            enriched = await ensure_member_with_presence(guild, target_member.id)
            if enriched is not None:
                target_member = enriched
        except Exception:
            pass

        try:
            fetched_member = await safe_get_member(guild, target_member.id, force_fetch=True)
            if fetched_member is not None:
                target_member = fetched_member
        except Exception:
            pass

        # Ensure we have a fully populated user for banner/accent
        try:
            full_user = await safe_get_user(self.bot, target_member.id, force_fetch=True)
            if full_user:
                # overwrite banner/accent fields if available
                target_member._user = full_user
        except Exception:
            pass

        score, details = evaluate_member(target_member, bot=self.bot)

        emb = build_inspection_embed(target_member, score, details)
        await interaction.followup.send(embed=emb, ephemeral=True)

    # ---------- Join hook ----------
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        try:
            # Read relevant settings (use defaults if unset)
            settings = await mysql.get_settings(member.guild.id, [
                "antibot-enabled",
                "antibot-min-score",
                "antibot-autorole",
                "antibot-autorole-min-score",
                "monitor-channel",
            ])
            enabled = bool(settings.get("antibot-enabled", False))
            min_score = int(settings.get("antibot-min-score", 0) or 0)
            autorole_id = settings.get("antibot-autorole")
            autorole_min = int(settings.get("antibot-autorole-min-score", 70) or 70)
            monitor_channel_id = settings.get("monitor-channel")

            # Try to enrich the join member with presence info (best-effort)
            try:
                enriched = await ensure_member_with_presence(member.guild, member.id)
                if enriched is not None:
                    member = enriched
            except Exception:
                pass

            try:
                fetched_member = await safe_get_member(member.guild, member.id, force_fetch=True)
                if fetched_member is not None:
                    member = fetched_member
            except Exception:
                pass

            try:
                full_user = await safe_get_user(self.bot, member.id, force_fetch=True)
                if full_user:
                    member._user = full_user
            except Exception:
                pass

            score, details = evaluate_member(member, bot=self.bot)

            # Always log a compact embed if monitor channel is configured
            if monitor_channel_id:
                emb = build_join_embed(member, score, details)
                await log_to_channel(emb, int(monitor_channel_id), self.bot)

            # Optional auto-role
            if autorole_id and score >= autorole_min and not member.pending:
                role = member.guild.get_role(int(autorole_id))
                if role:
                    try:
                        # Validate hierarchy/permissions implicitly via add_roles
                        await member.add_roles(role, reason=f"Auto role via AntiBot (score {score} >= {autorole_min})")
                    except (discord.Forbidden, discord.HTTPException):
                        # Report failure to monitor channel (if set)
                        if monitor_channel_id:
                            warn = discord.Embed(
                                title="AntiBot: Auto-role failed",
                                description=(
                                    f"Could not assign {role.mention} to {member.mention}.\n"
                                    "Check the bot role is above the target role and has Manage Roles."
                                ),
                                color=discord.Color.orange(),
                            )
                            try:
                                await log_to_channel(warn, int(monitor_channel_id), self.bot)
                            except Exception:
                                pass

            # Optional auto-kick with guardrails: require multiple strong indicators
            if enabled and min_score and score < min_score:
                severe_flags = 0
                # Strong negatives
                if details.get("default_avatar", False):
                    severe_flags += 1
                if details.get("membership_screening_pending", False):
                    severe_flags += 1
                if (details.get("account_age_days") or 9999) <= 3:
                    severe_flags += 1
                if (details.get("creation_to_join_minutes") or 9999) <= 60:
                    severe_flags += 1
                if (details.get("name_digits_ratio") or 0.0) >= 0.5 and (details.get("name_longest_digit_run") or 0) >= 5:
                    severe_flags += 1
                # roles removed as a pointer (do not count)

                if severe_flags >= 3:
                    try:
                        await member.kick(reason=f"Auto-kicked: low trust ({score}<{min_score}) and {severe_flags} strong indicators")
                    except (discord.Forbidden, discord.HTTPException):
                        pass

        except Exception:
            # Never let join handling crash other cogs
            return


async def setup(bot: commands.Bot):
    await bot.add_cog(AntiBotCog(bot))
