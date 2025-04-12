# settings_schema.py

from typing import Any, Callable, Optional
import discord

class Setting:
    def __init__(self, name: str, description: str, setting_type: type, default: Any = None, encrypted: bool = False, hidden: bool = False, validator: Optional[Callable] = None):
        self.name = name
        self.description = description
        self.type = setting_type
        self.default = default
        self.encrypted = encrypted
        self.hidden = hidden
        self.validator = validator

    def validate(self, value: Any) -> bool:
        if self.validator:
            return self.validator(value)
        return True

SETTINGS_SCHEMA = {
    "strike-channel": Setting(
        name="strike-channel",
        description="Channel where strikes are logged.",
        setting_type=discord.TextChannel,
    ),
    "nsfw-channel": Setting(
        name="nsfw-channel",
        description="Channel where NSFW violations are logged with a preview of the media.",
        setting_type=discord.TextChannel,
    ),
    "monitor-channel": Setting(
        name="monitor-channel",
        description="Channel to log all server activities, including message edits, deletions, and user join/leave events.",
        setting_type=discord.TextChannel,
    ),
    "delete-offensive": Setting(
        name="delete-offensive",
        description="Automatically delete messages containing offensive content, such as harassment or hate speech.",
        setting_type=bool,
        default=False,
    ),
    "delete-nsfw": Setting(
        name="delete-nsfw",
        description="Automatically delete messages containing NSFW content.",
        setting_type=bool,
        default=True,
    ),
    "strike-nsfw": Setting(
        name="strike-nsfw",
        description="Strike users for sending NSFW content.",
        setting_type=bool,
        default=True,
    ),
    "restrict-striked-users": Setting(
        name="restrict-striked-users",
        description="Restrict striked users by scanning their messages for offensive content (TEXT ONLY).",
        setting_type=bool,
        default=False,
    ),
    "exclude-channels": Setting(
        name="exclude-channels",
        description="Channels to exclude from detection.",
        setting_type=list[discord.TextChannel],
        default=[],
    ),
    "api-key": Setting(
        name="api-key",
        description="OPENAI API key for NSFW detection.",
        setting_type=str,
        default=None,
        encrypted=True,
    ),
    # strike actions, type is dict[int, str]
    "strike-actions": Setting(
        name="strike-actions",
        description="Actions to take for each strike level.",
        setting_type=dict[str, tuple[str, str]],
        hidden=True,
        default={
            "1": ("timeout", "1d"),
            "2": ("timeout", "7d"),
            "3": ("ban", "-1"), 
        },
    ),
    "opt-in": Setting(
        name="opt-in",
        description="Set to False for all users to not have data tracked in your server.",
        setting_type=dict[str, tuple[str, str]],
        hidden=True,
        default=True
    ),
}