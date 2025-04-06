from discord.ext import commands
from discord import app_commands, Interaction
from modules.utils.mysql import execute_query
import io
import re
import discord

class banned_words(commands.Cog):
    """A cog for banned words handling and relevant commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # Add a banned word
    @app_commands.command(
        name="add_banned_word",
        description="Add a word to the banned words list."
    )
    @app_commands.checks.has_permissions(moderate_members=True)
    async def add_banned_word(self, interaction: Interaction, word: str):
        """Add a word to the banned words list."""
        guild_id = interaction.guild.id
        # Check if the word is already in the database
        existing_word = execute_query(
            "SELECT * FROM banned_words WHERE guild_id = %s AND word = %s",
            (guild_id, word),
            fetch_one=True
        )
        if existing_word[0]:
            await interaction.response.send_message(f"The word '{word}' is already banned.", ephemeral=True)
            return

        # Insert the new banned word into the database
        execute_query(
            "INSERT INTO banned_words (guild_id, word) VALUES (%s, %s)",
            (guild_id, word)
        )
        await interaction.response.send_message(f"The word '{word}' has been added to the banned words list.", ephemeral=True)

    # Remove a banned word
    @app_commands.command(
        name="remove_banned_word",
        description="Remove a word from the banned words list."
    )
    @app_commands.checks.has_permissions(moderate_members=True)
    async def remove_banned_word(self, interaction: Interaction, word: str):
        """Remove a word from the banned words list."""
        guild_id = interaction.guild.id
        # Check if the word exists in the database
        rows, _ = execute_query(
            "SELECT * FROM banned_words WHERE guild_id = %s AND word = %s",
            (guild_id, word),
            fetch_one=True
        )
        existing_word = rows[0] if rows else None
        if not existing_word:
            await interaction.response.send_message(f"The word '{word}' is not banned.", ephemeral=True)
            return

        # Remove the banned word from the database
        execute_query(
            "DELETE FROM banned_words WHERE guild_id = %s AND word = %s",
            (guild_id, word)
        )
        await interaction.response.send_message(f"The word '{word}' has been removed from the banned words list.", ephemeral=True)
    
    # List banned words (in a text file)
    @app_commands.command(
        name="list_banned_words",
        description="List all banned words."
    )
    @app_commands.checks.has_permissions(moderate_members=True)
    async def list_banned_words(self, interaction: Interaction):
        """List all banned words."""
        guild_id = interaction.guild.id
        # Retrieve all banned words from the database
        rows, _ = execute_query(
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

    # Logic for banning words in messages
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        guild_id = message.guild.id
        # unpack rows, ignore count
        rows, _ = execute_query(
            "SELECT word FROM banned_words WHERE guild_id = %s",
            (guild_id,),
            fetch_all=True
        )

        banned_words = [row[0] for row in rows]

        if not banned_words:
            return

        # build a regex that only matches whole words, case‑insensitive
        pattern = r'\b(?:' + '|'.join(re.escape(w) for w in banned_words) + r')\b'
        if re.search(pattern, message.content, re.IGNORECASE):
            await message.delete()
            await message.channel.send(
                f"{message.author.mention}, your message contained a banned word and was removed."
            )

        # important: let commands still be processed
        await self.bot.process_commands(message)

async def setup(bot: commands.Bot):
    await bot.add_cog(banned_words(bot))
