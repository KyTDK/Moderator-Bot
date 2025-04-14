from discord import app_commands, Interaction
from discord.ext import commands
from modules.utils.mysql import execute_query
from cryptography.fernet import Fernet
import os
from dotenv import load_dotenv

load_dotenv()

FERNET_KEY = os.getenv("FERNET_SECRET_KEY") 
fernet = Fernet(FERNET_KEY)



class ApiPoolCog(commands.Cog):
    """A cog for managing the API pool."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    api_pool_group = app_commands.Group(
        name="api_pool",
        description="Management of your personal API keys in the pool.",
    )

    @api_pool_group.command(
        name="explanation",
        description="Explain the API pool."
    )
    async def explain(self, interaction: Interaction):
        explanation = (
            "**What is the API Pool?**\n"
            "The API Pool is a secure and anonymous collection of shared OpenAI API keys. "
            "It enables Moderator Bot to continue operating effectively, even when individual users face rate limits.\n\n"
            "**How does it work?**\n"
            "- When you add your OpenAI API key to the pool, it's encrypted and stored securely.\n"
            "- The bot uses these keys exclusively for accessing OpenAI models to perform moderation tasks.\n"
            "- Your API key remains confidential; no one, including the bot developers, can view it.\n"
            "- You have full control and can remove your API key from the pool at any time.\n\n"
            "By contributing your API key, you help ensure that Moderator Bot remains responsive and effective for all users."
        )
        await interaction.response.send_message(explanation, ephemeral=True)

    @api_pool_group.command(
        name="add",
        description="Add an API key to the pool."
    )
    async def add_api(self, interaction: Interaction, api_key: str):
        user_id = interaction.user.id
        query = "INSERT INTO api_pool (user_id, api_key) VALUES (%s, %s)"
        _, affected_rows = execute_query(query, (user_id, fernet.encrypt(api_key.encode()).decode()))
        if affected_rows > 0:
            await interaction.response.send_message("API key added to your pool.", ephemeral=True)
        else:
            await interaction.response.send_message("This API key already exists in your pool.", ephemeral=True)

    @api_pool_group.command(
        name="remove",
        description="Remove an API key from the pool."
    )
    async def remove_api(self, interaction: Interaction, api_key: str):
        user_id = interaction.user.id
        query = "DELETE FROM api_pool WHERE user_id = %s AND api_key = %s"
        _, affected_rows = execute_query(query, (user_id, api_key))
        if affected_rows > 0:
            await interaction.response.send_message("API key removed from your pool.", ephemeral=True)
        else:
            await interaction.response.send_message("This API key was not found in your pool. Use /api_pool list to see your currnet api keys.", ephemeral=True)

    @api_pool_group.command(
        name="clear",
        description="Clear all your API keys from the pool."
    )
    async def clear_api(self, interaction: Interaction):
        user_id = interaction.user.id
        query = "DELETE FROM api_pool WHERE user_id = %s"
        _, affected_rows = execute_query(query, (user_id,))
        if affected_rows > 0:
            await interaction.response.send_message("All API keys have been cleared from your pool.", ephemeral=True)
        else:
            await interaction.response.send_message("No API keys found to clear.", ephemeral=True)

    @api_pool_group.command(
        name="list",
        description="List all API keys in your pool."
    )
    async def list_apis(self, interaction: Interaction):
        user_id = interaction.user.id
        query = "SELECT api_key FROM api_pool WHERE user_id = %s"
        result, _ = execute_query(query, (user_id,), fetch_all=True)
        if result:
            api_keys = [row[0] for row in result]
            formatted_keys = '\n'.join(f"- {key}" for key in api_keys)
            await interaction.response.send_message(f"Your API Keys:\n{formatted_keys}", ephemeral=True)
        else:
            await interaction.response.send_message("No API keys found in your pool.", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(ApiPoolCog(bot))