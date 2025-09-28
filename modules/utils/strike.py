from __future__ import annotations

from collections.abc import Iterable, Callable
from typing import Any

from discord import Role, Interaction

from modules.utils import time

DURATION_ACTIONS = {"timeout"}
ROLE_ACTIONS = {"give_role", "take_role"}
BASE_KEY = "modules.utils.strike.validation"
TranslateFn = Callable[..., Any]

def _format_message(
    translator: TranslateFn | None,
    key: str,
    fallback: str,
    *,
    placeholders: dict[str, Any] | None = None,
) -> str:
    placeholders = placeholders or {}
    message = fallback.format(**placeholders)
    if translator is None:
        return message
    return translator(
        f"{BASE_KEY}.{key}",
        placeholders=placeholders,
        fallback=message,
    )

async def validate_action(
    interaction: Interaction,
    action: str | None,
    *,
    duration: str | None = None,
    role: Role | None = None,
    valid_actions: Iterable[str] = (),
    allow_duration: bool = True,
    timeout_required: bool = True,
    ephemeral: bool = True,
    param: str | None = None,
    translator: TranslateFn | None = None,
) -> str | None:
    valid_set = [a.lower() for a in valid_actions]
    if not action or action.lower() not in valid_set:
        return await _return_errors(
            interaction,
            [
                _format_message(
                    translator,
                    "invalid_action",
                    "Action must be one of: {options}",
                    placeholders={"options": ", ".join(valid_set)},
                )
            ],
            ephemeral,
        )

    action = action.lower()
    errors: list[str] = []

    if action == "timeout":
        if not allow_duration:
            errors.append(
                _format_message(
                    translator,
                    "duration_not_allowed",
                    "Duration is not allowed for this command.",
                )
            )
        if timeout_required and not duration:
            errors.append(
                _format_message(
                    translator,
                    "duration_required",
                    "You must provide a duration (e.g. `30m`, `1d`, `2w`).",
                )
            )
        elif duration and not time.parse_duration(duration):
            errors.append(
                _format_message(
                    translator,
                    "duration_invalid",
                    "Invalid duration format. Use `20s`, `30m`, `2h`, `5d`, `2w`, `1mo`, `1y`.",
                )
            )
        if role:
            errors.append(
                _format_message(
                    translator,
                    "timeout_role_disallowed",
                    "You cannot attach a role to a timeout action.",
                )
            )
        return (
            f"{action}:{duration}"
            if not errors
            else await _return_errors(interaction, errors, ephemeral)
        )

    if action in ROLE_ACTIONS:
        if not role:
            errors.append(
                _format_message(
                    translator,
                    "role_required",
                    "You must specify a role for `{action}`.",
                    placeholders={"action": action},
                )
            )
        if duration:
            errors.append(
                _format_message(
                    translator,
                    "duration_not_supported",
                    "`{action}` does not support a duration.",
                    placeholders={"action": action},
                )
            )
        return (
            f"{action}:{role.id}"
            if not errors
            else await _return_errors(interaction, errors, ephemeral)
        )

    if action == "warn":
        if not param:
            errors.append(
                _format_message(
                    translator,
                    "warn_message_required",
                    "You must provide a warning message.",
                )
            )
        if duration:
            errors.append(
                _format_message(
                    translator,
                    "duration_not_supported",
                    "`{action}` does not support a duration.",
                    placeholders={"action": action},
                )
            )
        if role:
            errors.append(
                _format_message(
                    translator,
                    "warn_role_disallowed",
                    "You cannot attach a role to a warn action.",
                )
            )
        return (
            f"{action}:{param}"
            if not errors
            else await _return_errors(interaction, errors, ephemeral)
        )

    if duration:
        errors.append(
            _format_message(
                translator,
                "duration_not_supported",
                "`{action}` does not support a duration.",
                placeholders={"action": action},
            )
        )
    if role:
        errors.append(
            _format_message(
                translator,
                "role_not_supported",
                "`{action}` does not use a role.",
                placeholders={"action": action},
            )
        )
    return action if not errors else await _return_errors(interaction, errors, ephemeral)

async def _return_errors(interaction: Interaction, errors: list[str], ephemeral: bool):
    await interaction.followup.send("\n".join(errors), ephemeral=ephemeral)
    return None
