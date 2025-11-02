from __future__ import annotations

from discord import app_commands, Interaction

from modules.utils.localization import TranslateFn, localize_message
from modules.utils.mysql import get_settings, update_settings

BASE_KEY = "modules.utils.action_manager"


class ActionListManager:
    def __init__(self, setting_key: str):
        self.setting_key = setting_key

    @staticmethod
    def _sanitize_actions(actions: list[str]) -> tuple[list[str], bool]:
        cleaned: list[str] = []
        changed = False
        for entry in actions:
            base, _, param = entry.partition(":")
            if base.lower() == "broadcast":
                channel_part, sep, message = param.partition("|")
                if not sep or not channel_part.isdigit() or not message:
                    changed = True
                    continue
            cleaned.append(entry)
        return cleaned, changed

    async def add_action(
        self,
        guild_id: int,
        new_action: str,
        *,
        translator: TranslateFn | None = None,
    ) -> str:
        actions = await get_settings(guild_id, self.setting_key) or []
        if not isinstance(actions, list):
            actions = [actions]

        actions, removed_invalid = self._sanitize_actions(actions)
        if removed_invalid:
            await update_settings(guild_id, self.setting_key, actions)

        normalized = [a.split(":")[0] for a in actions]
        if new_action.split(":")[0] in normalized:
            return localize_message(
                translator,
                BASE_KEY,
                "add.duplicate",
                placeholders={"action": new_action},
                fallback="`{action}` is already in the list.",
            )

        actions.append(new_action)
        await update_settings(guild_id, self.setting_key, actions)
        return localize_message(
            translator,
            BASE_KEY,
            "add.success",
            placeholders={"action": new_action},
            fallback="Added `{action}` to actions.",
        )

    async def remove_action(
        self,
        guild_id: int,
        action: str,
        *,
        translator: TranslateFn | None = None,
    ) -> str:
        actions = await get_settings(guild_id, self.setting_key) or []
        actions = actions if isinstance(actions, list) else [actions]

        actions, removed_invalid = self._sanitize_actions(actions)
        if removed_invalid:
            await update_settings(guild_id, self.setting_key, actions)

        # Exact match attempt
        if ":" in action:
            if action not in actions:
                return localize_message(
                    translator,
                    BASE_KEY,
                    "remove.specific_missing",
                    placeholders={"action": action},
                    fallback="Action `{action}` does not exist.",
                )
            updated = [a for a in actions if a != action]
            await update_settings(guild_id, self.setting_key, updated)
            return localize_message(
                translator,
                BASE_KEY,
                "remove.specific_success",
                placeholders={"action": action},
                fallback="Removed specific action `{action}`.",
            )

        # General match (e.g., all `warn`)
        base = action
        matched = [a for a in actions if a.split(":")[0] == base]
        if not matched:
            return localize_message(
                translator,
                BASE_KEY,
                "remove.base_missing",
                placeholders={"action": base},
                fallback="No actions found for `{action}`.",
            )
        updated = [a for a in actions if a.split(":")[0] != base]
        await update_settings(guild_id, self.setting_key, updated)
        return localize_message(
            translator,
            BASE_KEY,
            "remove.base_success",
            placeholders={"action": base},
            fallback="Removed all `{action}` actions.",
        )

    async def view_actions(self, guild_id: int) -> list:
        actions = await get_settings(guild_id, self.setting_key) or []
        actions = actions if isinstance(actions, list) else [actions]
        actions, removed_invalid = self._sanitize_actions(actions)
        if removed_invalid:
            await update_settings(guild_id, self.setting_key, actions)
        return actions

    async def autocomplete(self, interaction: Interaction, current: str) -> list[app_commands.Choice[str]]:
        actions = await self.view_actions(interaction.guild.id)
        return [
            app_commands.Choice(name=action, value=action)
            for action in actions if current.lower() in action.lower()
        ][:25]
