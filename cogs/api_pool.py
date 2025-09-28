from discord import app_commands, Interaction
from discord.ext import commands
from modules.utils.mysql import execute_query
from modules.utils import api
from cryptography.fernet import Fernet
import os
from dotenv import load_dotenv
import hashlib


load_dotenv()

FERNET_KEY = os.getenv("FERNET_SECRET_KEY") 
fernet = Fernet(FERNET_KEY)

# nosec B303  # API key hashing, not password hashing
def compute_api_key_hash(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()

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
        explanation = self.bot.translate(
            "cogs.api_pool.explanation.body"
        )
        await interaction.response.send_message(explanation, ephemeral=True)

    @api_pool_group.command(
        name="add",
        description="Add an API key to the pool."
    )
    async def add_api(self, interaction: Interaction, api_key: str):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id
        api_key_hash = compute_api_key_hash(api_key)
        
        # Check if the hash already exists in the database
        existing, _ = await execute_query(
            "SELECT 1 FROM api_pool WHERE api_key_hash = %s",
            (api_key_hash,), fetch_one=True
        )

        try:
            await api.check_openai_api_key(api_key)
        except Exception as e:
            message = self.bot.translate(
                "cogs.api_pool.add.invalid",
                placeholders={"error": str(e)},
            )
            await interaction.followup.send(message, ephemeral=True)
            return

        if existing:
            await interaction.followup.send(
                self.bot.translate("cogs.api_pool.add.duplicate"),
                ephemeral=True,
            )
        else:
            # Insert the hash into the database
            await execute_query(
                "INSERT INTO api_pool (user_id, api_key, api_key_hash) VALUES (%s, %s, %s)",
                (user_id, fernet.encrypt(api_key.encode()).decode(), api_key_hash)
            )
            await interaction.followup.send(
                self.bot.translate("cogs.api_pool.add.success"),
                ephemeral=True,
            )

    @api_pool_group.command(
        name="remove",
        description="Remove an API key from the pool."
    )
    async def remove_api(self, interaction: Interaction, api_key: str):
        user_id = interaction.user.id
        query = "DELETE FROM api_pool WHERE user_id = %s AND api_key_hash = %s"
        _, affected_rows = await execute_query(query, (user_id, compute_api_key_hash(api_key)))
        if affected_rows > 0:
            await interaction.response.send_message(
                self.bot.translate("cogs.api_pool.remove.success"),
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                self.bot.translate("cogs.api_pool.remove.missing"),
                ephemeral=True,
            )

    @api_pool_group.command(
        name="clear",
        description="Clear all your API keys from the pool."
    )
    async def clear_api(self, interaction: Interaction):
        user_id = interaction.user.id
        query = "DELETE FROM api_pool WHERE user_id = %s"
        _, affected_rows = await execute_query(query, (user_id,))
        if affected_rows > 0:
            await interaction.response.send_message(
                self.bot.translate("cogs.api_pool.clear.success"),
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                self.bot.translate("cogs.api_pool.clear.empty"),
                ephemeral=True,
            )

    @api_pool_group.command(
        name="list",
        description="List all API keys in your pool (encrypted)."
    )
    async def list_apis(self, interaction: Interaction):
        user_id = interaction.user.id
        query = "SELECT api_key FROM api_pool WHERE user_id = %s"
        result, _ = await execute_query(query, (user_id,), fetch_all=True)
        if result:
            api_keys = [row[0] for row in result]
            formatted_keys = '\n'.join(f"- {fernet.decrypt(key.encode()).decode()}" for key in api_keys)
            header = self.bot.translate("cogs.api_pool.list.header")
            await interaction.response.send_message(
                f"{header}\n{formatted_keys}",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                self.bot.translate("cogs.api_pool.list.empty"),
                ephemeral=True,
            )

async def setup(bot: commands.Bot):
    await bot.add_cog(ApiPoolCog(bot))