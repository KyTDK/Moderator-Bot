from modules.utils.mysql import get_settings, update_settings

class ActionListManager:
    def __init__(self, setting_key: str):
        self.setting_key = setting_key

    async def add_action(self, guild_id: int, new_action: str) -> str:
        actions = await get_settings(guild_id, self.setting_key) or []
        if not isinstance(actions, list):
            actions = [actions]

        normalized = [a.split(":")[0] for a in actions]
        if new_action.split(":")[0] in normalized:
            return f"`{new_action}` is already in the list."

        actions.append(new_action)
        await update_settings(guild_id, self.setting_key, actions)
        return f"Added `{new_action}` to actions."

    async def remove_action(self, guild_id: int, action: str) -> str:
        actions = await get_settings(guild_id, self.setting_key) or []

        normalized = [a.split(":")[0] for a in actions]
        if action not in normalized:
            return f"`{action}` is not in the list."

        updated = [a for a in actions if a.split(":")[0] != action]
        await update_settings(guild_id, self.setting_key, updated)
        return f"Removed `{action}` from actions."

    async def view_actions(self, guild_id: int) -> list:
        actions = await get_settings(guild_id, self.setting_key) or []
        return actions if isinstance(actions, list) else [actions]