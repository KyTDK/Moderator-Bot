from modules.utils.mysql import get_settings, update_settings
from discord import app_commands, Interaction

class EventListManager:
    def __init__(self, setting_key: str):
        self.setting_key = setting_key

    async def add_event(self, guild_id: int, event_key: str, action: str) -> str:
        settings = await get_settings(guild_id, self.setting_key) or {}

        actions = settings.get(event_key, [])
        if action in actions:
            return f"`{action}` is already set for `{event_key}`."

        actions.append(action)
        settings[event_key] = actions
        await update_settings(guild_id, self.setting_key, settings)
        return f"Added `{action}` to `{event_key}`."

    async def remove_event_action(self, guild_id: int, event_key: str, action: str) -> str:
        settings = await get_settings(guild_id, self.setting_key) or {}

        actions = settings.get(event_key, [])
        if action not in actions:
            return f"`{action}` is not set for `{event_key}`."

        actions.remove(action)
        if actions:
            settings[event_key] = actions
        else:
            del settings[event_key]

        await update_settings(guild_id, self.setting_key, settings)
        return f"Removed `{action}` from `{event_key}`."

    async def clear_events(self, guild_id: int) -> str:
        await update_settings(guild_id, self.setting_key, {})
        return "All adaptive events have been cleared."

    async def view_events(self, guild_id: int) -> dict:
        return await get_settings(guild_id, self.setting_key) or {}

    async def autocomplete_event(self, interaction: Interaction, current: str) -> list[app_commands.Choice[str]]:
        settings = await self.view_events(interaction.guild.id)
        return [
            app_commands.Choice(name=k, value=k)
            for k in settings.keys() if current.lower() in k.lower()
        ][:25]
