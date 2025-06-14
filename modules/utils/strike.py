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
) -> str | None:
    """
    Validates an action (and optionally a duration or role).
    Returns a formatted action string or None on failure.
    """

    if not action or action.lower() not in valid_actions:
        await interaction.followup.send(
            f"Action must be one of: {', '.join(valid_actions)}", ephemeral=ephemeral
        )
        return None

    action = action.lower()
    errors = []

    # Timeout-specific logic
    if action == "timeout":
        if not allow_duration:
            errors.append("Duration is not allowed for this command.")
        if timeout_required and not duration:
            errors.append("You must provide a duration for timeouts (e.g. `30m`, `1d`, `2w`).")
        elif duration and not time.parse_duration(duration):
            errors.append("Invalid duration format. Use `20s`, `30m`, `2h`, `5d`, `2w`, `1mo`, `1y`.")
        if role:
            errors.append("You cannot attach a role to a timeout action.")

        if errors:
            await interaction.followup.send("\n".join(errors), ephemeral=ephemeral)
            return None

        return f"{action}:{duration}"

    # Role-based logic
    if action in ROLE_ACTIONS:
        if not role:
            errors.append(f"You must specify a role for `{action}`.")
        if duration:
            errors.append(f"You cannot attach a duration to `{action}`.")

        if errors:
            await interaction.followup.send("\n".join(errors), ephemeral=ephemeral)
            return None

        return f"{action}:{role.id}"

    # Generic action (e.g. ban, kick, strike, delete)
    if duration:
        errors.append(f"`{action}` does not support a duration.")
    if role:
        errors.append(f"`{action}` does not use a role.")

    if errors:
        await interaction.followup.send("\n".join(errors), ephemeral=ephemeral)
        return None

    return action