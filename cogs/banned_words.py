import unicodedata
from discord.ext import commands
from discord import app_commands, Interaction
from modules.moderation import strike
from modules.utils.action_manager import ActionListManager
from modules.utils.mysql import execute_query
import io
import re
import discord
from better_profanity import profanity
from cleantext import clean
from modules.utils import mysql
from modules.utils.strike import validate_action
from modules.utils.actions import action_choices, VALID_ACTION_VALUES

BANNED_ACTION_SETTING = "banned-words-action"
manager = ActionListManager(BANNED_ACTION_SETTING)

RE_REPEATS = re.compile(r"(.)\1{2,}")
def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")

    # Remove user mentions and channel mentions
    text = re.sub(r"<[@#]!?[0-9]+>", "", text)

    # Flatten repeated characters to a maximum of two
    text = RE_REPEATS.sub(r"\1\1", text)

    text = clean(
        text,
        lower=True,
        to_ascii=True,
        no_line_breaks=True,
        no_urls=True,
        no_emails=True,
        no_phone_numbers=True,
        no_digits=False,
        no_currency_symbols=True,
        no_punct=True,
        lang="en"
    )

    text = re.sub(r'\s+', ' ', text).strip() 
    return text

class BannedWordsCog(commands.Cog):

    """A cog for banned words handling and relevant commands."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
    
    bannedwords_group = app_commands.Group(
        name="bannedwords",
        description="Banned words management commands.",
        default_permissions=discord.Permissions(manage_messages=True),
        guild_only=True
    )

    @bannedwords_group.command(
        name="defaults",
        description="Enable or disable the built-in profanity list."
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="enable",  value="true"),
            app_commands.Choice(name="disable", value="false"),
            app_commands.Choice(name="status",  value="status"),
        ]
    )
    async def set_default_scan(self, interaction: Interaction, action: str):
        """Toggle or view the default-list setting for this guild."""
        guild_id = interaction.guild.id

        if action == "status":
            current = await mysql.get_settings(guild_id, "use-default-banned-words")
            state   = "enabled" if current is not False else "disabled"
            await interaction.response.send_message(
                f"The built-in profanity list is **{state}**.", ephemeral=True
            )
            return

        new_value = (action == "true")
        await mysql.update_settings(guild_id, "use-default-banned-words", new_value)

        await interaction.response.send_message(
            f"Built-in profanity list **{'enabled' if new_value else 'disabled'}**.",
            ephemeral=True
        )

    @bannedwords_group.command(
        name="add",
        description="Add a word to the banned words list."
    )
    async def add_banned_word(self, interaction: Interaction, word: str):
        """Add a word to the banned words list."""
        guild_id = interaction.guild.id
        # Check if the word is already in the database
        existing_word, _ = await execute_query(
            "SELECT * FROM banned_words WHERE guild_id = %s AND word = %s",
            (guild_id, word),
            fetch_one=True
        )
        if existing_word:
            await interaction.response.send_message(f"The word '{word}' is already banned.", ephemeral=True)
            return

        # Insert the new banned word into the database
        await execute_query(
            "INSERT INTO banned_words (guild_id, word) VALUES (%s, %s)",
            (guild_id, word)
        )
        await interaction.response.send_message(f"The word '{word}' has been added to the banned words list.", ephemeral=True)

    # Remove a banned word
    @bannedwords_group.command(
        name="remove",
        description="Remove a word from the banned words list."
    )
    async def remove_banned_word(self, interaction: Interaction, word: str):
        """Remove a word from the banned words list."""
        guild_id = interaction.guild.id
        # Check if the word exists in the database
        rows, _ = await execute_query(
            "SELECT * FROM banned_words WHERE guild_id = %s AND word = %s",
            (guild_id, word),
            fetch_one=True
        )
        existing_word = rows[0] if rows else None
        if not existing_word:
            await interaction.response.send_message(f"The word '{word}' is not banned.", ephemeral=True)
            return

        # Remove the banned word from the database
        await execute_query(
            "DELETE FROM banned_words WHERE guild_id = %s AND word = %s",
            (guild_id, word)
        )
        await interaction.response.send_message(f"The word '{word}' has been removed from the banned words list.", ephemeral=True)
    @remove_banned_word.autocomplete("word")
    async def banned_word_autocomplete(
        self,
        interaction: Interaction,
        current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete banned words for the remove command."""
        guild_id = interaction.guild.id
        rows, _ = await execute_query(
            "SELECT word FROM banned_words WHERE guild_id = %s",
            (guild_id,),
            fetch_all=True
        )

        all_words = [row[0] for row in rows]
        filtered = [w for w in all_words if current.lower() in w.lower()]
        return [app_commands.Choice(name=word, value=word) for word in filtered[:25]]

    # List banned words (in a text file)
    @bannedwords_group.command(
        name="list",
        description="List all banned words."
    )
    async def list_banned_words(self, interaction: Interaction):
        """List all banned words."""
        guild_id = interaction.guild.id
        # Retrieve all banned words from the database
        rows, _ = await execute_query(
                    "SELECT word FROM banned_words WHERE guild_id = %s",
                    (guild_id,),
                    fetch_all=True
                )

        banned_words = [row[0] for row in rows]

        if not banned_words or len(banned_words) == 0:
            await interaction.response.send_message("No banned words found.", ephemeral=True)
            return

        file_content = "Banned Words:\n"
        if banned_words and len(banned_words) > 0:
            for word in banned_words:
                file_content += f"- {word}\n"
            file_buffer = io.StringIO(file_content)
            file = discord.File(file_buffer, filename = f"banned_words_{interaction.guild.id}.txt")

        await interaction.response.send_message(file=file, ephemeral=True)

    # Clear all banned words
    @bannedwords_group.command(
        name="clear",
        description="Clear all banned words."
    )
    async def clear_banned_words(self, interaction: Interaction):
        """Clear all banned words."""
        guild_id = interaction.guild.id
        # Clear all banned words from the database
        _, affected_rows = await execute_query(
            "DELETE FROM banned_words WHERE guild_id = %s",
            (guild_id,)
        )
        if affected_rows == 0:
            await interaction.response.send_message("No banned words found to clear.", ephemeral=True)
            return
        await interaction.response.send_message("All banned words have been cleared.", ephemeral=True)

    async def handle_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        guild_id = message.guild.id

        use_defaults = await mysql.get_settings(guild_id, "use-default-banned-words") == True

        rows, _ = await execute_query(
            "SELECT word FROM banned_words WHERE guild_id = %s", (guild_id,), fetch_all=True
        )
        custom = [r[0].lower() for r in rows]

        if use_defaults:
            profanity.load_censor_words()
            if custom:
                profanity.add_censor_words(custom)
        elif custom:
            profanity.load_censor_words(custom)
        else:
            return  # No banned words to check against

        normalised = normalize_text(message.content.lower())
        collapsed = normalised.replace(" ", "")

        if not (
            profanity.contains_profanity(normalised)
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
                    reason="Message contained banned word",
                    source="banned word",
                    message=message
                )
            except Exception:
                pass

        try:
            await message.channel.send(
                f"{message.author.mention}, your message contained a banned word and was removed."
            )
        except discord.Forbidden:
            pass

    @bannedwords_group.command(name="add_action", description="Add a moderation action to be triggered when a banned word is detected.")
    @app_commands.describe(
        action="Action to perform",
        duration="Only required for timeout (e.g. 10m, 1h, 3d)"
    )
    @app_commands.choices(action=action_choices())
    async def add_banned_action(
        self,
        interaction: Interaction,
        action: str,
        duration: str = None,
        role: discord.Role = None
    ):
        await interaction.response.defer(ephemeral=True)
        
        action_str = await validate_action(
            interaction=interaction,
            action=action,
            duration=duration,
            role=role,
            valid_actions=VALID_ACTION_VALUES,
        )
        if action_str is None:
            return

        msg = await manager.add_action(interaction.guild.id, action_str)
        await interaction.response.send_message(msg, ephemeral=True)

    @bannedwords_group.command(name="remove_action", description="Remove a specific action from the list of punishments for banned words.")
    @app_commands.describe(action="Exact action string to remove (e.g. timeout, delete)")
    @app_commands.choices(action=action_choices())
    async def remove_banned_action(self, interaction: Interaction, action: str):
        msg = await manager.remove_action(interaction.guild.id, action)
        await interaction.response.send_message(msg, ephemeral=True)

    @bannedwords_group.command(name="view_actions", description="Show all actions currently configured to trigger when banned words are used.")
    async def view_banned_actions(self, interaction: Interaction):
        actions = await manager.view_actions(interaction.guild.id)
        if not actions:
            await interaction.response.send_message("No actions are currently set for banned words.", ephemeral=True)
            return

        formatted = "\n".join(f"{i+1}. `{a}`" for i, a in enumerate(actions))
        await interaction.response.send_message(
            f"**Current banned words actions:**\n{formatted}",
            ephemeral=True
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(BannedWordsCog(bot))
