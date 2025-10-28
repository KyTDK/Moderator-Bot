from discord.ext import commands
from discord import app_commands, Interaction
from modules.moderation import strike
from modules.utils.action_manager import ActionListManager
import io
import discord
from modules.utils import mod_logging, mysql
from modules.utils.strike import validate_action
from modules.utils.actions import action_choices, VALID_ACTION_VALUES
from modules.utils.guild_list_storage import (
    GuildListAddResult,
    add_value,
    clear_values,
    fetch_values,
    remove_value,
)
from modules.utils.url_utils import ensure_scheme, extract_urls, norm_domain, norm_url
from urllib.parse import urlparse
from modules.core.moderator_bot import ModeratorBot
from modules.i18n.strings import locale_string

MAX_URLS = 500
BANNEDURLS_ACTION_SETTING = "url-detection-action"
manager = ActionListManager(BANNEDURLS_ACTION_SETTING)

def _is_domain_only(s: str) -> bool:
    p = urlparse(ensure_scheme(s))
    return (p.path or "").strip("/") == "" and not p.query and not p.fragment

class BannedURLsCog(commands.Cog):
    def __init__(self, bot: ModeratorBot):
        self.bot = bot
    
    bannedurls_group = app_commands.Group(
        name="bannedurls",
        description=locale_string("cogs.banned_urls.meta.group_description"),
        default_permissions=discord.Permissions(manage_messages=True),
        guild_only=True
    )

    @bannedurls_group.command(
        name="add",
        description=locale_string("cogs.banned_urls.meta.add.description"),
    )
    async def add_banned_urls(self, interaction: Interaction, url: str):
        guild_id = interaction.guild.id

        result = await add_value(
            guild_id=guild_id,
            table="banned_urls",
            column="url",
            value=url,
            limit=MAX_URLS,
        )
        if result is GuildListAddResult.ALREADY_PRESENT:
            await interaction.response.send_message(
                self.bot.translate(
                    "cogs.banned_urls.add.duplicate",
                    placeholders={"url": url},
                    guild_id=guild_id,
                ),
                ephemeral=True,
            )
            return
        if result is GuildListAddResult.LIMIT_REACHED:
            await interaction.response.send_message(
                self.bot.translate(
                    "cogs.banned_urls.limit_reached",
                    placeholders={"limit": MAX_URLS},
                    guild_id=guild_id,
                ),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            self.bot.translate(
                "cogs.banned_urls.add.success",
                placeholders={"url": url},
                guild_id=guild_id,
            ),
            ephemeral=True,
        )

    @bannedurls_group.command(
        name="remove",
        description=locale_string("cogs.banned_urls.meta.remove.description"),
    )
    async def remove_banned_urls(self, interaction: Interaction, url: str):
        guild_id = interaction.guild.id

        removed = await remove_value(
            guild_id=guild_id,
            table="banned_urls",
            column="url",
            value=url,
        )
        if not removed:
            await interaction.response.send_message(
                self.bot.translate("cogs.banned_urls.remove.missing",
                                   placeholders={"url": url},
                                   guild_id=guild_id),
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                self.bot.translate("cogs.banned_urls.remove.success", 
                                   placeholders={"url": url},
                                   guild_id=guild_id),
                ephemeral=True,
            )

    @remove_banned_urls.autocomplete("url")
    async def banned_urls_autocomplete(self, interaction: Interaction, current: str) -> list[app_commands.Choice[str]]:
        guild_id = interaction.guild.id
        all_urls = await fetch_values(
            guild_id=guild_id,
            table="banned_urls",
            column="url",
        )
        filtered = [u for u in all_urls if current.lower() in u.lower()]
        return [app_commands.Choice(name=u, value=u) for u in filtered[:25]]

    @bannedurls_group.command(
        name="list",
        description=locale_string("cogs.banned_urls.meta.list.description"),
    )
    async def list_banned_urls(self, interaction: Interaction):
        guild_id = interaction.guild.id
        banned = await fetch_values(
            guild_id=guild_id,
            table="banned_urls",
            column="url",
        )
        if not banned:
            await interaction.response.send_message(
                self.bot.translate("cogs.banned_urls.list.empty",
                                   guild_id=guild_id),
                ephemeral=True,
            )
            return
        header = self.bot.translate("cogs.banned_urls.list.file_header",
                                    guild_id=guild_id,)
        lines = [self.bot.translate("cogs.banned_urls.list.file_item", 
                                    placeholders={"url": u},
                                    guild_id=guild_id) for u in banned]
        buf = io.StringIO(header + "\n" + "\n".join(lines))
        filename = self.bot.translate("cogs.banned_urls.list.filename", 
                                      placeholders={"guild_id": interaction.guild.id},
                                      guild_id=guild_id)
        file = discord.File(buf, filename=filename)
        await interaction.response.send_message(file=file, ephemeral=True)

    @bannedurls_group.command(
        name="clear",
        description=locale_string("cogs.banned_urls.meta.clear.description"),
    )
    async def clear_banned_urls(self, interaction: Interaction):
        guild_id = interaction.guild.id
        removed = await clear_values(
            guild_id=guild_id,
            table="banned_urls",
        )
        if removed == 0:
            await interaction.response.send_message(
                self.bot.translate("cogs.banned_urls.clear.empty",
                                   guild_id=guild_id),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            self.bot.translate("cogs.banned_urls.clear.success",
                               guild_id=guild_id,),
            ephemeral=True,
        )

    async def handle_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        guild_id = message.guild.id

        # Exclude channels
        if message.channel.id in [int(c) for c in (await mysql.get_settings(guild_id, "exclude-url-channels") or [])]:
            return

        # Load banned
        banned = await fetch_values(
            guild_id=guild_id,
            table="banned_urls",
            column="url",
        )
        banned = [u for u in banned if u]
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
                    reason=self.bot.translate("cogs.banned_urls.enforcement.strike_reason", 
                                              placeholders={"url": matched_url},
                                              guild_id=guild_id),
                    source=self.bot.translate("cogs.banned_urls.enforcement.strike_source",
                                              guild_id=guild_id),
                    message=message,
                )
            except Exception:
                pass

        try:
            embed = discord.Embed(
                title=self.bot.translate("cogs.banned_urls.enforcement.embed_title",
                                         guild_id=guild_id),
                description=self.bot.translate("cogs.banned_urls.enforcement.embed_description", 
                                               placeholders={"mention": message.author.mention},
                                               guild_id=guild_id),
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

    @bannedurls_group.command(
        name="add_action",
        description=locale_string("cogs.banned_urls.meta.actions.add.description"),
    )
    @app_commands.describe(
        action=locale_string("cogs.banned_urls.meta.actions.add.options.action"),
        duration=locale_string("cogs.banned_urls.meta.actions.add.options.duration"),
    )
    @app_commands.choices(action=action_choices())
    async def add_banned_action(self, interaction: Interaction, action: str, duration: str = None, role: discord.Role = None, reason: str = None):
        await interaction.response.defer(ephemeral=True)
        action_str = await validate_action(interaction=interaction, action=action, duration=duration, role=role, valid_actions=VALID_ACTION_VALUES, param=reason, translator=self.bot.translate)
        if action_str is None:
            return
        msg = await manager.add_action(interaction.guild.id, action_str, translator=self.bot.translate)
        await interaction.followup.send(msg, ephemeral=True)

    @bannedurls_group.command(
        name="remove_action",
        description=locale_string("cogs.banned_urls.meta.actions.remove.description"),
    )
    @app_commands.describe(
        action=locale_string("cogs.banned_urls.meta.actions.remove.options.action")
    )
    @app_commands.autocomplete(action=manager.autocomplete)
    async def remove_banned_action(self, interaction: Interaction, action: str):
        msg = await manager.remove_action(interaction.guild.id, action, translator=self.bot.translate)
        await interaction.response.send_message(msg, ephemeral=True)

    @bannedurls_group.command(
        name="view_actions",
        description=locale_string("cogs.banned_urls.meta.actions.view.description"),
    )
    async def view_banned_actions(self, interaction: Interaction):
        guild_id = interaction.guild.id
        actions = await manager.view_actions(interaction.guild.id)
        if not actions:
            await interaction.response.send_message(
                self.bot.translate("cogs.banned_urls.actions.none",
                                   guild_id=guild_id),
                ephemeral=True,
            )
            return
        formatted = "\n".join(f"{i+1}. `{a}`" for i, a in enumerate(actions))
        header = self.bot.translate("cogs.banned_urls.actions.header", 
                                    placeholders={"actions": formatted},
                                    guild_id=guild_id)
        await interaction.response.send_message(header, ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(BannedURLsCog(bot))