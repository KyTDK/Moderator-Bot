from modules.utils import time

async def validate_action_with_duration(
    interaction,
    action: str | None,
    duration: str | None,
    valid_actions: list[str],
    allow_duration: bool = True,
    timeout_required: bool = True,
    ephemeral: bool = True,
):
    """
    Validates an action and optional duration. Returns (formatted_action_string | None) on success, sends error reply otherwise.
    """
    if not action or action.lower() not in valid_actions:
        await interaction.response.send_message(
            f"Action must be one of: {', '.join(valid_actions)}", ephemeral=ephemeral
        )
        return None

    action = action.lower()

    if action == "timeout":
        if not allow_duration:
            await interaction.response.send_message(
                "Duration is not supported for this command.", ephemeral=ephemeral
            )
            return None
        if timeout_required and not duration:
            await interaction.response.send_message(
                "You must provide a duration for timeouts (e.g. `30m`, `1d`, `2w`).", ephemeral=ephemeral
            )
            return None
        if duration and not time.parse_duration(duration):
            await interaction.response.send_message(
                "Invalid duration format. Use `20s`, `30m`, `2h`, `5d`, `2w`, `1mo`, `1y` etc.", ephemeral=ephemeral
            )
            return None

    return f"{action}:{duration}" if duration else action
