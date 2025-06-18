from modules.utils import api
from typing import Any, Callable, Optional
import discord
from modules.variables.TimeString import TimeString

class Setting:
    def __init__(
        self,
        name: str,
        description: str,
        setting_type: type,
        default: Any = None,
        encrypted: bool = False,
        hidden: bool = False,
        private: bool = False,
        validator: Optional[Callable] = None,
        choices: Optional[list[str]] = None,  # New field
    ):
        self.name = name
        self.description = description
        self.type = setting_type
        self.default = default
        self.encrypted = encrypted
        self.hidden = hidden
        self.private = private
        self.validator = validator
        self.choices = choices

    async def validate(self, value: Any):
        if self.validator:
            await self.validator(value)

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
    "nsfw-detection-action": Setting(
        name="nsfw-detection-action",
        description="Action to take when a user posts NSFW content.",
        setting_type=list[str],
        default=["delete", "strike"],
        hidden=True,
        choices=["strike", "kick", "ban", "timeout", "delete"]
    ),
    "banned-words-action": Setting(
        name="banned-words-action",
        description="Action to take when a user posts a banned word.",
        setting_type=list[str],
        default=["delete"],
        hidden=True,
        choices=["strike", "kick", "ban", "timeout", "delete"]
    ),
    "monitor-channel": Setting(
        name="monitor-channel",
        description="Channel to log all server activities, including message edits, deletions, and user join/leave events.",
        setting_type=discord.TextChannel,
    ),
    "cycle-strike-actions": Setting(
        name="cycle-strike-actions",
        description="Cycle through strike actions when run out of actions to give user.",
        setting_type=bool,
        default=True,
        choices=["true", "false"]
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
        private=True,
        validator=api.check_openai_api_key,
        encrypted=True,
    ),
    "strike-actions": Setting(
        name="strike-actions",
        description="Actions to take for each strike level.",
        setting_type=dict[str, list[str]],
        hidden=True,
        default={
            "1": ["timeout:1d"],
            "2": ["timeout:7d"],
            "3": ["ban"],
        },
    ),
    "strike-expiry": Setting(
        name="strike-expiry",
        description="Time a strike lasts for. Use formats like 20s, 30m, 2h, 30d, 2w, 5mo, 1y. Seconds, minutes, hours, days, weeks, months and years respectively.",
        setting_type=TimeString,
        default=None
    ),
    "dm-on-strike": Setting(
        name="dm-on-strike",
        description="DM the user when they receive a strike.",
        setting_type=bool,
        default=True,
        choices=["true", "false"]
    ),
    "check-pfp": Setting(
        name="check-pfp",
        description="Check the profile picture of the user for NSFW content.",
        setting_type=bool,
        default=False, # False by default to avoid unnecessary API calls
        choices=["true", "false"]
    ),
    "nsfw-pfp-action": Setting(
        name="nsfw-pfp-action",
        description="Action to take when a user sets an NSFW profile picture.",
        setting_type=list[str],
        default=["kick"],
        choices=["strike", "strike:2d", "kick", "ban", "timeout:1d", "timeout:7d"],
    ),
    "nsfw-pfp-message": Setting(
        name="nsfw-pfp-message",
        description="The message to send when a player has had their profile picture flagged.",
        setting_type=str,
        default="Your profile picture was detected to contain explicit content",
        choices=[
            "Your profile picture was detected to contain explicit content",
            "Your profile picture is not appropriate for this server.",
            "Your profile picture has been flagged as NSFW.",
        ]
    ),
    "unmute-on-safe-pfp": Setting(
        name="unmute-on-safe-pfp",
        description="Remove timeout of a user once they have changed their profile picture to something appropriate.",
        setting_type=bool,
        default=False,
        choices=["true", "false"]
    ),
    "use-default-banned-words": Setting(
        name="use-default-banned-words",
        description="Use the built-in profanity list for this server.",
        setting_type=bool,
        default=False,
        hidden=True,
        choices=["true", "false"]
    ),
    "delete-scam-messages": Setting(
        name="delete-scam-messages",
        description="Automatically delete messages that match scam patterns or URLs.",
        setting_type=bool,
        default=True,
        hidden=True,
        choices=["true", "false"]
    ),
    "scam-detection-action": Setting(
        name="scam-detection-action",
        description="Action to take when a scam message is detected.",
        setting_type=list[str],
        hidden=True,
        default=["delete"],
        choices=["strike", "kick", "ban", "timeout", "delete"],
    ),
    "ai-scam-detection": Setting(
        name="ai-scam-detection",
        description="Use AI to detect scam messages.",
        setting_type=bool,
        default=False,
        hidden=True,
        choices=["true", "false"]
    ),
    "check-links": Setting(
        name="check-links",
        description="Check links in messages for malware, phishing, scamming etc.",
        setting_type=bool,
        default=True,
        hidden=True,
        choices=["true", "false"]
    ),
    "exclude-scam-channels": Setting(
        name="exclude-scam-channels",
        description="Channels to exclude from scam detection.",
        setting_type=list[discord.TextChannel],
        default=[],
        hidden=True,
    ),
    "rules": Setting(
        name="rules",
        description="The server rules used for autonomous moderation.",
        setting_type=str,
        default=None,
        hidden=True,
    ),
    "aimod-detection-action": Setting(
        name="aimod-detection-action",
        description="Action to take when the autonomous moderator detects a violation. Use auto for the AI to make the decision.",
        setting_type=list[str],
        hidden=True,
        default=["auto"],
        choices=["strike", "kick", "ban", "timeout", "delete", "auto"],
    ),
    "autonomous-mod": Setting(
        name="autonomous-mod",
        description="Use AI to automatically moderate your entire server.",
        setting_type=bool,
        default=False,
        hidden=True,
        choices=["true", "false"]
    ),
    "aimod-model": Setting(
        name="aimod-model",
        description="Which OpenAI model to use for AI moderation.",
        setting_type=str,
        default="gpt-4.1-nano",
        choices=[
            "gpt-4.1",
            "gpt-4.1-mini",
            "gpt-4.1-nano",
            "gpt-4o",
            "gpt-4o-mini",
        ]
    ),
    "aimod-check-interval": Setting(
        name="aimod-check-interval",
        description="How often to run the AI moderation batch process.",
        setting_type=TimeString,
        default=TimeString("10m"),
    ),
    "contextual-ai": Setting(
        name="contextual-ai",
        description="Allow the AI moderator to consider prior messages and replies for context.",
        setting_type=bool,
        default=False,
        choices=["true", "false"]
    )
}