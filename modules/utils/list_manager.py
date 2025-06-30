from modules.utils.mysql import get_settings, update_settings
from discord import app_commands, Interaction

class ListManager:
    def __init__(self, setting_key: str):
        self.setting_key = setting_key

    async def add(self, guild_id: int, item: str) -> str:
        items = await get_settings(guild_id, self.setting_key) or []
        if not isinstance(items, list):
            items = [items]
        if item in items:
            return f"`{item}` is already in the list."
        items.append(item)
        await update_settings(guild_id, self.setting_key, items)
        return f"Added `{item}` to the list."

    async def remove(self, guild_id: int, item: str) -> str:
        items = await get_settings(guild_id, self.setting_key) or []
        if item not in items:
            return f"`{item}` is not in the list."
        items.remove(item)
        await update_settings(guild_id, self.setting_key, items)
        return f"Removed `{item}` from the list."

    async def view(self, guild_id: int) -> list[str]:
        items = await get_settings(guild_id, self.setting_key) or []
        return items if isinstance(items, list) else [items]

    async def autocomplete(self, interaction: Interaction, current: str) -> list[app_commands.Choice[str]]:
        items = await self.view(interaction.guild.id)
        return [
            app_commands.Choice(name=item, value=item)
            for item in items if current.lower() in item.lower()
        ][:25]
