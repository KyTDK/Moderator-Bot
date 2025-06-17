from modules.utils.mysql import get_settings, update_settings

class StrikeActionManager:
    def __init__(self, setting_key: str = "strike-actions"):
        self.setting_key = setting_key

    async def add_action(self, guild_id: int, strike: int | str, new_action: str) -> str:
        actions_map = await get_settings(guild_id, self.setting_key) or {}
        strike = str(strike)
        current = actions_map.get(strike, [])
        normalized = [a.split(":")[0] for a in current]
        if new_action.split(":")[0] in normalized:
            return f"`{new_action}` is already set for `{strike}` strike(s)."
        current.append(new_action)
        actions_map[strike] = current
        await update_settings(guild_id, self.setting_key, actions_map)
        return f"Added `{new_action}` for `{strike}` strike(s)."

    async def remove_action(self, guild_id: int, strike: int | str, action: str) -> str:
        actions_map = await get_settings(guild_id, self.setting_key) or {}
        strike = str(strike)
        current = actions_map.get(strike, [])
        normalized = [a.split(":")[0] for a in current]
        if action not in normalized:
            return f"`{action}` is not set for `{strike}` strike(s)."
        updated = [a for a in current if a.split(":")[0] != action]
        if updated:
            actions_map[strike] = updated
        else:
            actions_map.pop(strike, None)
        await update_settings(guild_id, self.setting_key, actions_map)
        return f"Removed `{action}` from `{strike}` strike(s)."

    async def view_actions(self, guild_id: int) -> dict:
        return await get_settings(guild_id, self.setting_key) or {}
