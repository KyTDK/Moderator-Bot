import discord
from discord.ext import commands
from discord import app_commands, Interaction
from modules.utils import mysql
from modules.core.moderator_bot import ModeratorBot
from modules.i18n.strings import locale_string

LOG_CHANNEL_TYPES: dict[str, tuple[str, str, str]] = {
    "strike": ("strike-channel", "Strike", "cogs.channel_config.meta.types.strike"),
    "nsfw": ("nsfw-channel", "NSFW", "cogs.channel_config.meta.types.nsfw"),
    "ai": ("aimod-channel", "AI", "cogs.channel_config.meta.types.ai"),
    "monitor": ("monitor-channel", "Monitor", "cogs.channel_config.meta.types.monitor"),
    "captcha": ("captcha-log-channel", "Captcha", "cogs.channel_config.meta.types.captcha"),
    "vc_transcript": (
        "vcmod-transcript-channel",
        "VC Transcript",
        "cogs.channel_config.meta.types.vc_transcript",
    ),
}


def _channel_type_choices() -> list[app_commands.Choice[str]]:
    return [
        app_commands.Choice(
            name=locale_string(translation_key, default=default_label),
            value=identifier,
        )
        for identifier, (_, default_label, translation_key) in LOG_CHANNEL_TYPES.items()
    ]


class ChannelConfigCog(commands.Cog):
    def __init__(self, bot: ModeratorBot): self.bot = bot

    channels_group = app_commands.Group(
        name="channels",
        description=locale_string("cogs.channel_config.meta.group_description"),
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @channels_group.command(
        name="set",
        description=locale_string("cogs.channel_config.meta.set.description"),
    )
    @app_commands.describe(
        channel=locale_string("cogs.channel_config.meta.set.params.channel"),
        type=locale_string("cogs.channel_config.meta.set.params.type"),
    )
    @app_commands.choices(type=_channel_type_choices())
    async def set_channel(
        self,
        interaction: Interaction,
        channel: discord.TextChannel,
        type: app_commands.Choice[str],
    ):
        guild_id = interaction.guild.id
        setting_key, _, translation_key = LOG_CHANNEL_TYPES[type.value]
        type_label = self.bot.translate(translation_key, 
                                        fallback=type.name,
                                        guild_id=guild_id,)

        if type.value == "nsfw" and not channel.is_nsfw():
            await interaction.response.send_message(
                self.bot.translate("cogs.channel_config.nsfw_required", 
                                   placeholders={"channel": channel.mention},
                                   guild_id=guild_id,),
                ephemeral=True,
            )
            return

        required_perms = [
            "view_channel",
            "send_messages",
            "embed_links",
            "attach_files",
        ]
        perms = channel.permissions_for(interaction.guild.me)

        missing = [p for p in required_perms if not getattr(perms, p)]
        if missing:
            perm_list = ", ".join(m.replace('_', ' ').title() for m in missing)
            await interaction.response.send_message(
                self.bot.translate("cogs.channel_config.missing_permissions", 
                                   placeholders={"channel": channel.mention, "permissions": perm_list},
                                   guild_id=guild_id,),
                ephemeral=True,
            )
            return

        await mysql.update_settings(interaction.guild.id, setting_key, channel.id)
        await interaction.response.send_message(
            self.bot.translate(
                "cogs.channel_config.set_success",
                placeholders={"log_type": type_label, "channel": channel.mention},
                guild_id=guild_id,
            ),
            ephemeral=True,
        )

    @channels_group.command(
        name="unset",
        description=locale_string("cogs.channel_config.meta.unset.description"),
    )
    @app_commands.describe(
        type=locale_string("cogs.channel_config.meta.unset.params.type")
    )
    @app_commands.choices(type=_channel_type_choices())
    async def unset_channel(self, interaction: Interaction, type: app_commands.Choice[str]):
        setting_key, _, translation_key = LOG_CHANNEL_TYPES[type.value]
        guild_id = interaction.guild.id
        type_label = self.bot.translate(translation_key, fallback=type.name)
        await mysql.update_settings(interaction.guild.id, setting_key, None)
        await interaction.response.send_message(
            self.bot.translate(
                "cogs.channel_config.unset_success",
                placeholders={"log_type": type_label},
                guild_id=guild_id,
            ),
            ephemeral=True,
        )

    @channels_group.command(
        name="show",
        description=locale_string("cogs.channel_config.meta.show.description"),
    )
    async def show_channels(self, interaction: Interaction):
        show_texts = self.bot.translate("cogs.channel_config.show")
        # Ensure guild ID is an int
        guild_id = interaction.guild.id
        if isinstance(guild_id, str):
            try:
                guild_id = int(guild_id)
            except ValueError:
                await interaction.response.send_message(
                    self.bot.translate("cogs.channel_config.invalid_guild"), ephemeral=True
                )
                return

        settings = await mysql.get_settings(
            guild_id,
            [config[0] for config in LOG_CHANNEL_TYPES.values()]
        ) or {}

        def fmt(identifier: str, config: tuple[str, str, str]):
            setting_key, default_label, translation_key = config
            cid = settings.get(setting_key)
            if isinstance(cid, str):
                try:
                    cid = int(cid)
                except ValueError:
                    cid = None
            ch = interaction.guild.get_channel(cid) if cid else None
            value = ch.mention if ch else show_texts["not_set"]
            return self.bot.translate(
                "cogs.channel_config.show.entry",
                placeholders={
                    "name": self.bot.translate(
                        translation_key,
                        fallback=default_label,
                        guild_id=guild_id,
                    ),
                    "value": value,
                },
                guild_id=guild_id,
            )

        lines = [fmt(identifier, config) for identifier, config in LOG_CHANNEL_TYPES.items()]
        await interaction.response.send_message(
            show_texts["heading"] + "\n" + "\n".join(lines),
            ephemeral=True
        )



async def setup(bot: commands.Bot):
    await bot.add_cog(ChannelConfigCog(bot))