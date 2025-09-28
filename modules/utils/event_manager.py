from __future__ import annotations

from discord import app_commands, Interaction

from modules.utils.localization import TranslateFn, localize_message
from modules.utils.mysql import get_settings, update_settings

BASE_KEY = "modules.utils.event_manager"


class EventListManager:
    def __init__(self, setting_key: str):
        self.setting_key = setting_key

    async def add_event(
        self,
        guild_id: int,
        event_key: str,
        action: str,
        *,
        translator: TranslateFn | None = None,
    ) -> str:
        settings = await get_settings(guild_id, self.setting_key) or {}

        actions = settings.get(event_key, [])
        if action in actions:
            return localize_message(
                translator,
                BASE_KEY,
                "add.duplicate",
                placeholders={"action": action, "event": event_key},
                fallback="`{action}` is already set for `{event}`.",
            )

        actions.append(action)
        settings[event_key] = actions
        await update_settings(guild_id, self.setting_key, settings)
        return localize_message(
            translator,
            BASE_KEY,
            "add.success",
            placeholders={"action": action, "event": event_key},
            fallback="Added `{action}` to `{event}`.",
        )

    async def remove_event_action(
        self,
        guild_id: int,
        event_key: str,
        action: str,
        *,
        translator: TranslateFn | None = None,
    ) -> str:
        settings = await get_settings(guild_id, self.setting_key) or {}

        actions = settings.get(event_key, [])
        if action not in actions:
            return localize_message(
                translator,
                BASE_KEY,
                "remove.missing",
                placeholders={"action": action, "event": event_key},
                fallback="`{action}` is not set for `{event}`.",
            )

        actions.remove(action)
        if actions:
            settings[event_key] = actions
        else:
            settings.pop(event_key, None)

        await update_settings(guild_id, self.setting_key, settings)
        return localize_message(
            translator,
            BASE_KEY,
            "remove.success",
            placeholders={"action": action, "event": event_key},
            fallback="Removed `{action}` from `{event}`.",
        )

    async def clear_events(
        self,
        guild_id: int,
        *,
        translator: TranslateFn | None = None,
    ) -> str:
        await update_settings(guild_id, self.setting_key, {})
        return localize_message(
            translator,
            BASE_KEY,
            "clear.success",
            placeholders={},
            fallback="All adaptive events have been cleared.",
        )

    async def view_events(self, guild_id: int) -> dict:
        return await get_settings(guild_id, self.setting_key) or {}

    async def autocomplete_event(self, interaction: Interaction, current: str) -> list[app_commands.Choice[str]]:
        settings = await self.view_events(interaction.guild.id)
        return [
            app_commands.Choice(name=k, value=k)
            for k in settings.keys() if current.lower() in k.lower()
        ][:25]
