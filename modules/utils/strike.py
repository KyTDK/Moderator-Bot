from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - imported only for type checking
    from discord import Interaction, Role
else:  # pragma: no cover - fallback when discord isn't installed
    Interaction = Role = Any

from modules.moderation.action_specs import (
    ROLE_ACTION_CANONICAL,
    get_action_spec,
)
from modules.utils import time

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
    spec = get_action_spec(action)
    canonical_action = spec.canonical_name if spec else action
    errors: list[str] = []

    if canonical_action == "timeout":
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
            f"{canonical_action}:{duration}"
            if not errors
            else await _return_errors(interaction, errors, ephemeral)
        )

    if canonical_action in ROLE_ACTION_CANONICAL:
        if not role:
            errors.append(
                _format_message(
                    translator,
                    "role_required",
                    "You must specify a role for `{action}`.",
                    placeholders={"action": canonical_action},
                )
            )
        if duration:
            errors.append(
                _format_message(
                    translator,
                    "duration_not_supported",
                    "`{action}` does not support a duration.",
                    placeholders={"action": canonical_action},
                )
            )
        return (
            f"{canonical_action}:{role.id}"
            if not errors
            else await _return_errors(interaction, errors, ephemeral)
        )

    if canonical_action == "warn":
        if spec and spec.requires_message and not param:
            errors.append(
                _format_message(
                    translator,
                    spec.missing_message_key or "warn_message_required",
                    spec.missing_message_fallback
                    or "You must provide a warning message.",
                )
            )
        if duration:
            errors.append(
                _format_message(
                    translator,
                    "duration_not_supported",
                    "`{action}` does not support a duration.",
                    placeholders={"action": canonical_action},
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
            f"{canonical_action}:{param}"
            if not errors
            else await _return_errors(interaction, errors, ephemeral)
        )

    if duration:
        if not spec or not spec.allows_duration:
            errors.append(
                _format_message(
                    translator,
                    "duration_not_supported",
                    "`{action}` does not support a duration.",
                    placeholders={"action": canonical_action},
                )
            )
    elif spec and spec.requires_duration:
        errors.append(
            _format_message(
                translator,
                "duration_required",
                "You must provide a duration (e.g. `30m`, `1d`, `2w`).",
            )
        )

    if role:
        if not spec or not spec.allows_role:
            errors.append(
                _format_message(
                    translator,
                    "role_not_supported",
                    "`{action}` does not use a role.",
                    placeholders={"action": canonical_action},
                )
            )
    elif spec and spec.requires_role:
        errors.append(
            _format_message(
                translator,
                "role_required",
                "You must specify a role for `{action}`.",
                placeholders={"action": canonical_action},
            )
        )

    if spec and spec.requires_message and not param and canonical_action != "warn":
        errors.append(
            _format_message(
                translator,
                spec.missing_message_key or "message_required",
                spec.missing_message_fallback
                or "You must provide a message for `{action}`.",
                placeholders={"action": canonical_action},
            )
        )

    if errors:
        return await _return_errors(interaction, errors, ephemeral)

    if duration and spec and spec.allows_duration:
        return f"{canonical_action}:{duration}"

    if role and spec and spec.requires_role:
        return f"{canonical_action}:{role.id}"

    if param and spec and spec.requires_message:
        return f"{canonical_action}:{param}"

    return canonical_action

async def _return_errors(interaction: Interaction, errors: list[str], ephemeral: bool):
    await interaction.followup.send("\n".join(errors), ephemeral=ephemeral)
    return None
