from discord.ext import commands
from discord import app_commands, Interaction, Member, Embed, Color
from modules.utils.mysql import execute_query
from modules.utils.user_utils import has_role_or_permission
from discord.app_commands.errors import MissingPermissions
from modules.moderation import strike
import io
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
        if existing_word:
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
        existing_word = execute_query(
            "SELECT * FROM banned_words WHERE guild_id = %s AND word = %s",
            (guild_id, word),
            fetch_one=True
        )
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
        banned_words = execute_query(
            "SELECT word FROM banned_words WHERE guild_id = %s",
            (guild_id,),
            fetch_all=True
        )
        if not banned_words:
            await interaction.response.send_message("No banned words found.", ephemeral=True)
            return

        file_content = "Banned Words:\n"
        for word in banned_words:
            content += f"- {word[0]}\n"
        file_buffer = io.StringIO(file_content)
        file = discord.File(file_buffer, filename="banned_words.txt")

        await interaction.response.send_message(file=file, ephemeral=True)

    # Logic for banning words in messages
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        guild_id = message.guild.id
        banned_words = execute_query(
            "SELECT word FROM banned_words WHERE guild_id = %s",
            (guild_id,),
            fetch_all=True
        )
        if not banned_words:
            return

        for word in banned_words:
            if word[0] in message.content:
                await message.delete()
                break

async def setup(bot: commands.Bot):
    await bot.add_cog(banned_words(bot))
