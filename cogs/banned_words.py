import unicodedata
from discord.ext import commands
from discord import app_commands, Interaction
from modules.utils.mysql import execute_query
import io
import re
import discord
from cleantext import clean
from rapidfuzz.distance import Levenshtein
from rapidfuzz import fuzz

def is_match(normalized: str, banned: str) -> bool:
    if normalized == banned:
        return True

    distance = Levenshtein.distance(normalized, banned)
    max_len = max(len(normalized), len(banned))
    similarity = 1 - distance / max_len

    if similarity > 0.85:
        return True

    if normalized.startswith(banned) and len(normalized) <= len(banned) + 3:
        return True

    if len(normalized) >= 4 and normalized[0] == banned[0]:
        if fuzz.partial_ratio(normalized, banned) > 88:
            return True

    return False

RE_REPEATS = re.compile(r"(.)\1{2,}")
def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    
    # Early collapse
    text = RE_REPEATS.sub(r"\1\1", text)

    leet_map = {
        '1': 'i', '!': 'i', '|': 'i',
        '@': 'a', '$': 's', '5': 's',
        '0': 'o', '3': 'e', '7': 't',
        '+': 't', '9': 'g', '6': 'g'
    }
    for k, v in leet_map.items():
        text = text.replace(k, v)

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

    text = re.sub(r'\s+', '', text)
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
            file = discord.File(file_buffer, filename="banned_words.txt")

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

        rows, _ = await execute_query(
            "SELECT word FROM banned_words WHERE guild_id = %s",
            (guild_id,),
            fetch_all=True
        )

        banned_words = [row[0].lower() for row in rows]
        if not banned_words:
            return

        normalized = normalize_text(message.content)

        for banned in banned_words:
            # Fuzzy match with threshold
            if is_match(normalized, banned):
                break
        else:
            suffix  = r'(?:ed|er|ing)?'
            pattern = re.compile(
                r'\b(?:' + '|'.join(fr'{re.escape(w)}{suffix}' for w in banned_words) + r')\b',
                flags=re.I
            )
            if not pattern.search(message.content):
                return

        # Delete + notify
        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound):
            print(f"Could not delete message {message.id} in {message.channel.id}")

        try:
            await message.channel.send(
                f"{message.author.mention}, your message contained a banned word and was removed."
            )
        except discord.Forbidden:
            print(f"Missing permission to send message in {message.channel.id}")

async def setup(bot: commands.Bot):
    await bot.add_cog(BannedWordsCog(bot))
