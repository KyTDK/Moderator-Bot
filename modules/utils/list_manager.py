from __future__ import annotations

from discord import app_commands, Interaction

from modules.utils.localization import TranslateFn, localize_message
from modules.utils.mysql import get_settings, update_settings

BASE_KEY = "modules.utils.list_manager"


class ListManager:
    def __init__(self, setting_key: str, *, message_namespace: str | None = None):
        self.setting_key = setting_key
        self.message_namespace = message_namespace or BASE_KEY

    async def add(
        self,
        guild_id: int,
        item: str,
        *,
        translator: TranslateFn | None = None,
    ) -> str:
        items = await get_settings(guild_id, self.setting_key) or []
        if not isinstance(items, list):
            items = [items]
        if item in items:
            return localize_message(
                translator,
                self.message_namespace,
                "add.duplicate",
                placeholders={"item": item},
                fallback="`{item}` is already in the list.",
            )
        items.append(item)
        await update_settings(guild_id, self.setting_key, items)
        return localize_message(
            translator,
            self.message_namespace,
            "add.success",
            placeholders={"item": item},
            fallback="Added `{item}` to the list.",
        )

    async def remove(
        self,
        guild_id: int,
        item: str,
        *,
        translator: TranslateFn | None = None,
    ) -> str:
        items = await get_settings(guild_id, self.setting_key) or []
        if item not in items:
            return localize_message(
                translator,
                self.message_namespace,
                "remove.missing",
                placeholders={"item": item},
                fallback="`{item}` is not in the list.",
            )
        items.remove(item)
        await update_settings(guild_id, self.setting_key, items)
        return localize_message(
            translator,
            self.message_namespace,
            "remove.success",
            placeholders={"item": item},
            fallback="Removed `{item}` from the list.",
        )

    async def view(self, guild_id: int) -> list[str]:
        items = await get_settings(guild_id, self.setting_key) or []
        return items if isinstance(items, list) else [items]

    async def autocomplete(self, interaction: Interaction, current: str) -> list[app_commands.Choice[str]]:
        items = await self.view(interaction.guild.id)
        return [
            app_commands.Choice(name=item, value=item)
            for item in items if current.lower() in item.lower()
        ][:25]
