from discord.ext import commands
from discord import app_commands, Interaction
from modules.moderation import strike
from modules.utils.action_manager import ActionListManager
import io
import re
import discord
from better_profanity import profanity
from cleantext import clean
from modules.utils import mod_logging, mysql
from modules.utils.guild_list_storage import (
    GuildListAddResult,
    add_value,
    clear_values,
    fetch_values,
    remove_value,
)
from modules.utils.strike import validate_action
from modules.i18n.strings import locale_namespace
from modules.utils.actions import action_choices, VALID_ACTION_VALUES
from modules.utils.text import normalize_text
from modules.core.moderator_bot import ModeratorBot

MAX_BANNED_WORDS = 500
BANNED_ACTION_SETTING = "banned-words-action"
manager = ActionListManager(BANNED_ACTION_SETTING)

RE_REPEATS = re.compile(r"(.)\1{2,}")

LOCALE = locale_namespace("cogs", "banned_words")
META = LOCALE.child("meta")
DEFAULT_CHOICES = META.child("defaults", "choices")
ADD_ACTION_PARAMS = META.child("add_action", "params")
REMOVE_ACTION_PARAMS = META.child("remove_action", "params")

class BannedWordsCog(commands.Cog):

    """A cog for banned words handling and relevant commands."""
    def __init__(self, bot: ModeratorBot):
        self.bot = bot
    
    bannedwords_group = app_commands.Group(
        name="bannedwords",
        description=META.string("group_description"),
        default_permissions=discord.Permissions(manage_messages=True),
        guild_only=True
    )

    @bannedwords_group.command(
        name="defaults",
        description=META.string("defaults", "description"),
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(
                name=DEFAULT_CHOICES.string("enable"),
                value="true",
            ),
            app_commands.Choice(
                name=DEFAULT_CHOICES.string("disable"),
                value="false",
            ),
            app_commands.Choice(
                name=DEFAULT_CHOICES.string("status"),
                value="status",
            ),
        ]
    )
    async def set_default_scan(self, interaction: Interaction, action: str):
        """Toggle or view the default-list setting for this guild."""
        guild_id = interaction.guild.id

        if action == "status":
            current = await mysql.get_settings(guild_id, "use-default-banned-words")
            state_key = (
                "cogs.banned_words.defaults.state_enabled"
                if current is not False
                else "cogs.banned_words.defaults.state_disabled"
            )
            state = self.bot.translate(state_key,
                                       guild_id=guild_id)
            message = self.bot.translate(
                "cogs.banned_words.defaults.status",
                placeholders={"state": state},
                guild_id=guild_id
            )
            await interaction.response.send_message(message, ephemeral=True)
            return

        new_value = (action == "true")
        await mysql.update_settings(guild_id, "use-default-banned-words", new_value)
        state_key = (
            "cogs.banned_words.defaults.state_enabled"
            if new_value
            else "cogs.banned_words.defaults.state_disabled"
        )
        state = self.bot.translate(state_key,
                                   guild_id=guild_id)
        message = self.bot.translate(
            "cogs.banned_words.defaults.updated",
            placeholders={"state": state},
            guild_id=guild_id
        )
        await interaction.response.send_message(message, ephemeral=True)

    @bannedwords_group.command(
        name="add",
        description=META.string("add", "description"),
    )
    async def add_banned_word(self, interaction: Interaction, word: str):
        """Add a word to the banned words list."""
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild.id
        result = await add_value(
            guild_id=guild_id,
            table="banned_words",
            column="word",
            value=word,
            limit=MAX_BANNED_WORDS,
        )
        if result is GuildListAddResult.ALREADY_PRESENT:
            await interaction.followup.send(
                self.bot.translate(
                    "cogs.banned_words.add.duplicate",
                    placeholders={"word": word},
                    guild_id=guild_id,
                ),
                ephemeral=True,
            )
            return
        if result is GuildListAddResult.LIMIT_REACHED:
            await interaction.followup.send(
                self.bot.translate(
                    "cogs.banned_words.limit_reached",
                    placeholders={"limit": MAX_BANNED_WORDS},
                    guild_id=guild_id,
                ),
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            self.bot.translate(
                "cogs.banned_words.add.success",
                placeholders={"word": word},
                guild_id=guild_id,
            ),
            ephemeral=True,
        )

    # Remove a banned word
    @bannedwords_group.command(
        name="remove",
        description=META.string("remove", "description"),
    )
    async def remove_banned_word(self, interaction: Interaction, word: str):
        """Remove a word from the banned words list."""
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild.id
        # Check if the word exists in the database
        removed = await remove_value(
            guild_id=guild_id,
            table="banned_words",
            column="word",
            value=word,
        )
        if not removed:
            await interaction.followup.send(
                self.bot.translate("cogs.banned_words.remove.missing",
                                   placeholders={"word": word},
                                   guild_id=guild_id,),
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            self.bot.translate("cogs.banned_words.remove.success",
                               placeholders={"word": word},
                               guild_id=guild_id,),
            ephemeral=True,
        )
    @remove_banned_word.autocomplete("word")
    async def banned_word_autocomplete(
        self,
        interaction: Interaction,
        current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete banned words for the remove command."""
        guild_id = interaction.guild.id
        all_words = await fetch_values(
            guild_id=guild_id,
            table="banned_words",
            column="word",
        )
        filtered = [w for w in all_words if current.lower() in w.lower()]
        return [app_commands.Choice(name=word, value=word) for word in filtered[:25]]

    # List banned words (in a text file)
    @bannedwords_group.command(
        name="list",
        description=META.string("list", "description"),
    )
    async def list_banned_words(self, interaction: Interaction):
        """List all banned words."""
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild.id
        # Retrieve all banned words from the database
        banned_words = await fetch_values(
            guild_id=guild_id,
            table="banned_words",
            column="word",
        )

        if not banned_words or len(banned_words) == 0:
            await interaction.followup.send(
                self.bot.translate("cogs.banned_words.list.empty",
                                   guild_id=guild_id,),
                ephemeral=True,
            )
            return

        file_content = self.bot.translate("cogs.banned_words.list.file_header",
                                          guild_id=guild_id) + "\n"
        if banned_words and len(banned_words) > 0:
            for word in banned_words:
                file_content += self.bot.translate("cogs.banned_words.list.file_item", 
                                                   placeholders={"word": word},
                                                   guild_id=guild_id) + "\n"
            file_buffer = io.StringIO(file_content)
            file = discord.File(file_buffer, filename = f"banned_words_{interaction.guild.id}.txt")

        await interaction.followup.send(file=file, ephemeral=True)

    # Clear all banned words
    @bannedwords_group.command(
        name="clear",
        description=META.string("clear", "description"),
    )
    async def clear_banned_words(self, interaction: Interaction):
        """Clear all banned words."""
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild.id
        # Clear all banned words from the database
        removed = await clear_values(
            guild_id=guild_id,
            table="banned_words",
        )
        if removed == 0:
            await interaction.followup.send(
                self.bot.translate("cogs.banned_words.clear.empty",
                                   guild_id=guild_id,),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            self.bot.translate("cogs.banned_words.clear.success",
                               guild_id=guild_id,),
            ephemeral=True,
        )

    async def handle_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        guild_id = message.guild.id

        # Exclude channels
        if message.channel.id in [int(c) for c in (await mysql.get_settings(guild_id, "exclude-bannedwords-channels") or [])]:
            return

        use_defaults = await mysql.get_settings(guild_id, "use-default-banned-words")

        rows = await fetch_values(
            guild_id=guild_id,
            table="banned_words",
            column="word",
        )
        custom = [r.lower() for r in rows]

        if use_defaults:
            profanity.load_censor_words()
            if custom:
                profanity.add_censor_words(custom)
        elif custom:
            profanity.load_censor_words(custom)
        else:
            return  # No banned words to check against

        # Preserve URLs, mentions, and emojis when normalizing for banned-words checks
        normalised = normalize_text(
            message.content.lower(),
            remove_urls=False,
            remove_mentions=False,
            remove_custom_emojis=False,
            to_ascii=False,            # keep Unicode emojis
            remove_punct=True,
        )
        collapsed  = re.sub(r"[\W_]+", "", normalised)

        custom_words = [w.lower() for w in custom]
        has_custom_substring = any(
            (w in normalised) or (w in collapsed)
            for w in custom_words
        )

        if not (
            has_custom_substring
            or profanity.contains_profanity(normalised)
            or profanity.contains_profanity(collapsed)
        ):
            return

        action_flag = await mysql.get_settings(guild_id, BANNED_ACTION_SETTING)
        if action_flag:
            try:
                await strike.perform_disciplinary_action(
                    user=message.author,
                    bot=self.bot,
                    action_string=action_flag,
                    reason=self.bot.translate("cogs.banned_words.enforcement.strike_reason",
                                              guild_id=guild_id,),
                    source=self.bot.translate("cogs.banned_words.enforcement.strike_source",
                                              guild_id=guild_id,),
                    message=message
                )
            except Exception:
                pass

        try:
            embed = discord.Embed(
                title=self.bot.translate("cogs.banned_words.enforcement.embed_title",
                                         guild_id=guild_id,),
                description=self.bot.translate("cogs.banned_words.enforcement.embed_description", 
                                               placeholders={"mention": message.author.mention},
                                               guild_id=guild_id,),
                color=discord.Color.red()
            )
            embed.set_thumbnail(url=message.author.display_avatar.url)
            await mod_logging.log_to_channel(
                embed=embed,
                channel_id=message.channel.id,
                bot=self.bot
            )
        except discord.Forbidden:
            pass

    async def handle_message_edit(self, cached_before: dict, after: discord.Message):
        await self.handle_message(after)

    @bannedwords_group.command(
        name="add_action",
        description=META.string("add_action", "description"),
    )
    @app_commands.describe(
        action=ADD_ACTION_PARAMS.string("action"),
        duration=ADD_ACTION_PARAMS.string("duration"),
    )
    @app_commands.choices(action=action_choices())
    async def add_banned_action(
        self,
        interaction: Interaction,
        action: str,
        duration: str = None,
        role: discord.Role = None,
        reason: str = None,
    ):
        await interaction.response.defer(ephemeral=True)
        
        action_str = await validate_action(
            interaction=interaction,
            action=action,
            duration=duration,
            role=role,
            valid_actions=VALID_ACTION_VALUES,
            param=reason,
            translator=self.bot.translate,
        )
        if action_str is None:
            return

        msg = await manager.add_action(interaction.guild.id, action_str, translator=self.bot.translate)
        await interaction.followup.send(msg, ephemeral=True)

    @bannedwords_group.command(
        name="remove_action",
        description=META.string("remove_action", "description"),
    )
    @app_commands.describe(
        action=REMOVE_ACTION_PARAMS.string("action")
    )
    @app_commands.autocomplete(action=manager.autocomplete)
    async def remove_banned_action(self, interaction: Interaction, action: str):
        msg = await manager.remove_action(interaction.guild.id, action, translator=self.bot.translate)
        await interaction.response.send_message(msg, ephemeral=True)

    @bannedwords_group.command(
        name="view_actions",
        description=META.string("view_actions", "description"),
    )
    async def view_banned_actions(self, interaction: Interaction):
        actions = await manager.view_actions(interaction.guild.id)
        guild_id = interaction.guild.id
        if not actions:
            await interaction.response.send_message(
                self.bot.translate("cogs.banned_words.actions.none",
                                   guild_id=guild_id,),
                ephemeral=True,
            )
            return

        formatted = "\n".join(f"{i+1}. `{a}`" for i, a in enumerate(actions))
        header = self.bot.translate(
            "cogs.banned_words.actions.header",
            placeholders={"actions": formatted},
            guild_id=guild_id,
        )
        await interaction.response.send_message(header, ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(BannedWordsCog(bot))
