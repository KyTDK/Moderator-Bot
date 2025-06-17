from modules.utils import time
from discord import Role, Interaction

DURATION_ACTIONS = {"timeout"}
ROLE_ACTIONS = {"give_role", "take_role"}

async def validate_action(
    interaction: Interaction,
    action: str | None,
    duration: str | None = None,
    role: Role | None = None,
    valid_actions: list[str] = [],
    allow_duration: bool = True,
    timeout_required: bool = True,
    ephemeral: bool = True,
    param: str | None = None
) -> str | None:
    if not action or action.lower() not in valid_actions:
        return await _return_errors(interaction, [f"Action must be one of: {', '.join(valid_actions)}"], ephemeral)

    action = action.lower()
    errors = []

    if action == "timeout":
        if not allow_duration:
            errors.append("Duration is not allowed for this command.")
        if timeout_required and not duration:
            errors.append("You must provide a duration (e.g. `30m`, `1d`, `2w`).")
        elif duration and not time.parse_duration(duration):
            errors.append("Invalid duration format. Use `20s`, `30m`, `2h`, `5d`, `2w`, `1mo`, `1y`.")
        if role:
            errors.append("You cannot attach a role to a timeout action.")
        return f"{action}:{duration}" if not errors else await _return_errors(interaction, errors, ephemeral)

    if action in ROLE_ACTIONS:
        if not role:
            errors.append(f"You must specify a role for `{action}`.")
        if duration:
            errors.append(f"`{action}` does not support a duration.")
        return f"{action}:{role.id}" if not errors else await _return_errors(interaction, errors, ephemeral)

    if action == "warn":
        if not param:
            errors.append("You must provide a warning message.")
        if duration:
            errors.append(f"`{action}` does not support a duration.")
        if role:
            errors.append("You cannot attach a role to a warn action.")
        return f"{action}:{param}" if not errors else await _return_errors(interaction, errors, ephemeral)

    if duration:
        errors.append(f"`{action}` does not support a duration.")
    if role:
        errors.append(f"`{action}` does not use a role.")
    return action if not errors else await _return_errors(interaction, errors, ephemeral)

async def _return_errors(interaction: Interaction, errors: list[str], ephemeral: bool):
    await interaction.followup.send("\n".join(errors), ephemeral=ephemeral)
    return None