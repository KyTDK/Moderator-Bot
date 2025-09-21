from typing import Any, Callable, Optional
from collections.abc import Iterable
import discord
from modules.config.premium_plans import PLAN_CORE, PLAN_PRO, PLAN_ULTRA, resolve_required_plans
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
        choices: Optional[list[str]] = None,
        required_plans: str | Iterable[str] | None = None,
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

        normalized_plans = resolve_required_plans(required_plans) if required_plans is not None else None
        self.required_plans = frozenset(normalized_plans) if normalized_plans else None
        self.accelerated = bool(self.required_plans)

    async def validate(self, value: Any):
        if self.validator:
            await self.validator(value)

SETTINGS_SCHEMA = {
    "strike-channel": Setting(
        name="strike-channel",
        description="Channel where strikes are logged.",
        setting_type=discord.TextChannel,
        hidden=True
    ),
    "aimod-debug": Setting(
        name="aimod-debug",
        description="Always send a detailed AI moderation debug message to the AI violations channel, including flagged messages, AI decision, and applied actions.",
        setting_type=bool,
        default=False,
        choices=["true", "false"]
    ),
    "nsfw-verbose": Setting(
        name="nsfw-verbose",
        description="Post a detailed scan report embed in the same channel as the scanned media.",
        setting_type=bool,
        default=False,
        choices=["true", "false"]
    ),
    "nsfw-channel": Setting(
        name="nsfw-channel",
        description="Channel where NSFW violations are logged with a preview of the media.",
        setting_type=discord.TextChannel,
        hidden=True
    ),
    "nsfw-enabled": Setting(
        name="nsfw-enabled",
        description="Enable NSFW scanning for messages, reactions, and avatars (other toggles still apply).",
        setting_type=bool,
        default=True,
        choices=["true", "false"]
    ),
    "monitor-events": Setting(
        name="monitor-events",
        description="Enable/disable logging for each type of monitored event.",
        setting_type=dict[str, bool],
        default={
            "join": True,
            "leave": True,
            "ban": True,
            "kick": True,
            "unban": True,
            "timeout": True,
            "message_delete": True,
            "message_edit": True,
            },
        hidden=True
    ),
    "nsfw-detection-action": Setting(
        name="nsfw-detection-action",
        description="Action to take when a user posts NSFW content.",
        setting_type=list[str],
        default=["delete", "strike"],
        hidden=True,
        choices=["strike", "kick", "ban", "timeout", "delete"]
    ),
    "nsfw-detection-categories": Setting(
        name="nsfw-detection-categories",
        description="Categories considered NSFW for detection.",
        setting_type=list[str],
        default=[
            "violence_graphic",
            "sexual"
        ],
        required_plans=PLAN_CORE,
        hidden=True,
    ),
    "nsfw-high-accuracy": Setting(
        name="nsfw-high-accuracy",
        description="Enable high-accuracy NSFW scans for more reliable detection. Accelerated only.",
        setting_type=bool,
        default=False,
        required_plans=PLAN_CORE,
        choices=["true", "false"]
    ),
    "threshold": Setting(
        name="threshold",
        description="Threshold for NSFW detection confidence. Lower values are more sensitive.",
        setting_type=float,
        default=0.7,
        hidden=True
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
        hidden=True,
        description="Channel to log all server activities, including message edits, deletions, and user join/leave events.",
        setting_type=discord.TextChannel,
    ),
    "captcha-log-channel": Setting(
        name="captcha-log-channel",
        description="Channel where captcha verification logs and activity updates are posted.",
        setting_type=discord.TextChannel,
        hidden=True,
    ),
    "aimod-channel": Setting(
        name="aimod-channel",
        description="Channel where AI violation logs are posted.",
        setting_type=discord.TextChannel,
        hidden=True
    ),
    "cycle-strike-actions": Setting(
        name="cycle-strike-actions",
        description="Cycle through strike actions when run out of actions to give user.",
        setting_type=bool,
        default=True,
        choices=["true", "false"]
    ),
    "exclude-bannedwords-channels": Setting(
        name="exclude-bannedwords-channels",
        description="Channels to exclude from banned words detection.",
        setting_type=list[discord.TextChannel],
        default=[],
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
        choices=["true", "false"],
        required_plans=PLAN_CORE,
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
        ],
        required_plans=PLAN_CORE
    ),
    "unmute-on-safe-pfp": Setting(
        name="unmute-on-safe-pfp",
        description="Remove timeout of a user once they have changed their profile picture to something appropriate.",
        setting_type=bool,
        default=False,
        choices=["true", "false"],
        required_plans=PLAN_CORE
    ),
    "scan-age-restricted": Setting(
        name="scan-age-restricted",
        description="Scan age-restricted (NSFW) channels. When off, NSFW channels are skipped.",
        setting_type=bool,
        default=False,
        choices=["true", "false"]
    ),
    "check-tenor-gifs": Setting(
        name="check-tenor-gifs",
        description="Apply punishments when NSFW content is detected in Tenor GIFs.",
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
    "exclude-channels": Setting(
        name="exclude-channels",
        description="Channels to exclude from detection.",
        setting_type=list[discord.TextChannel],
        default=[],
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
    "check-links": Setting(
        name="check-links",
        description="Check links in messages for malware, phishing, scamming etc.",
        setting_type=bool,
        default=False,
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
        default="1. Be respectful — no harassment or hate speech.\n2. No NSFW or obscene content.\n3. No spam or excessive self-promotion.\n4. Follow Discord's Terms of Service.",
        hidden=True,
        required_plans=PLAN_CORE,
    ),
    "aimod-detection-action": Setting(
        name="aimod-detection-action",
        description="Action to take when the autonomous moderator detects a violation. Use auto for the AI to make the decision.",
        setting_type=list[str],
        hidden=True,
        default=["auto"],
        required_plans=PLAN_CORE,
        choices=["strike", "kick", "ban", "timeout", "delete", "auto"],
    ),
    "aimod-high-accuracy": Setting(
        name="aimod-high-accuracy",
        description=(
            "Use higher-accuracy AI moderation with gpt-5-mini (approx. 2.25 USD per 1M tokens). "
            "Consumes the monthly budget faster compared to the default."
        ),
        setting_type=bool,
        default=False,
        required_plans=PLAN_CORE,
        choices=["true", "false"]
    ),

    "autonomous-mod": Setting(
        name="autonomous-mod",
        description="Use AI to automatically moderate your entire server.",
        setting_type=bool,
        default=False,
        hidden=True,
        required_plans=PLAN_CORE,
        choices=["true", "false"]
    ),
    "aimod-check-interval": Setting(
        name="aimod-check-interval",
        description="How often to run the AI moderation batch process.",
        setting_type=TimeString,
        required_plans=PLAN_CORE,
        default=TimeString("1h"),
    ),
    "aimod-mode": Setting(
        name="aimod-mode",
        description="Choose how AI moderation is triggered: `report` mode (only on mention), `interval` mode (automatic background scanning), or `adaptive` mode (dynamically switches based on server activity, events, or configuration).",
        setting_type=str,
        default="report",
        required_plans=PLAN_CORE,
        choices=["interval", "report", "adaptive"],
        hidden=True
    ),
    "aimod-adaptive-events": Setting(
        name="aimod-adaptive-events",
        description=(
            "Configure which events trigger switching AI moderation modes. "
        ),
        setting_type=dict[str, list[str]], #[event, list of actions]
        required_plans=PLAN_CORE,
        default={
            "mass_join": ["enable_interval", "disable_report"],
            "mass_leave": ["enable_interval", "disable_report"],
            "guild_inactive": ["enable_report", "disable_interval"],
            "server_spike": ["enable_interval", "disable_report"],
        },
        hidden=True
    ),
    "no-forward-from-role": Setting(
        name="no-forward-from-role",
        description="Stop a specific role from forwarding messages.",
        setting_type=list[discord.Role],
        default=[]
    ),
    "banned-urls": Setting(
        name="blocked-urls",
        description="Exact URLs or domains to block.",
        setting_type=list[str],
        default=[],
    ),
    "url-detection-action": Setting(
        name="url-detection-action",
        description="Action to take when a blocked URL is detected.",
        setting_type=list[str],
        default=["delete"],
        choices=["delete", "strike", "kick", "ban", "timeout"],
    ),
    "exclude-url-channels": Setting(
        name="exclude-url-channels",
        description="Channels to exclude from banned URLs.",
        setting_type=list[discord.TextChannel],
        default=[],
    ),
    "vcmod-enabled": Setting(
        name="vcmod-enabled",
        description="Enable voice channel moderation and transcription.",
        setting_type=bool,
        default=False,
        hidden=True,
        required_plans=PLAN_CORE,
        choices=["true", "false"]
    ),
    "vcmod-channels": Setting(
        name="vcmod-channels",
        description="Voice channels to monitor (when empty, none are monitored).",
        setting_type=list[discord.VoiceChannel],
        default=[],
        hidden=True
    ),
    "vcmod-listen-duration": Setting(
        name="vcmod-listen-duration",
        description="How long to actively listen per voice channel during a cycle (e.g., 2m, 5m).",
        setting_type=TimeString,
        default=TimeString("2m"),
        required_plans=PLAN_CORE,
        hidden=True
    ),
    "vcmod-idle-duration": Setting(
        name="vcmod-idle-duration",
        description="How long to pause between each transcription in Saver Mode.",
        setting_type=TimeString,
        default=TimeString("30s"),
        required_plans=PLAN_CORE,
        hidden=True
    ),
    "vcmod-saver-mode": Setting(
        name="vcmod-saver-mode",
        description="Saver mode: bot cycles through VCs to appear active but does not record/listen most of the time.",
        setting_type=bool,
        default=False,
        hidden=True,
        required_plans=PLAN_CORE,
        choices=["true", "false"]
    ),
    "vcmod-rules": Setting(
        name="vcmod-rules",
        description="Rules applied to voice chat moderation (separate from text rules).",
        setting_type=str,
        default=(
            "1. No harassment, hate speech, or threats.\n"
            "2. No slurs or discriminatory language.\n"
            "3. No incitement to violence or illegal activity.\n"
            "4. No sexual content or explicit remarks.\n"
            "5. Follow Discord's Terms of Service."
        ),
        hidden=True,
        required_plans=PLAN_CORE,
    ),
    "vcmod-detection-action": Setting(
        name="vcmod-detection-action",
        description="Action to take when VC moderation detects a violation. Use auto for the AI to decide.",
        setting_type=list[str],
        default=["auto"],
        hidden=True,
        required_plans=PLAN_CORE,
        choices=["strike", "kick", "ban", "timeout", "delete", "auto"]
    ),
    "vcmod-high-accuracy": Setting(
        name="vcmod-high-accuracy",
        description="Use higher-accuracy AI analysis for VC moderation (uses more token budget).",
        setting_type=bool,
        default=False,
        hidden=True,
        required_plans=PLAN_CORE,
        choices=["true", "false"]
    ),
    "vcmod-transcript-channel": Setting(
        name="vcmod-transcript-channel",
        description="Channel where VC transcripts are posted.",
        setting_type=discord.TextChannel,
        hidden=True,
    ),
    "vcmod-transcript-only": Setting(
        name="vcmod-transcript-only",
        description=(
            "When enabled, the bot will only transcribe voice chat without applying AI rule checks "
            "or moderation actions. This significantly reduces budget usage."
        ),
        setting_type=bool,
        default=False,
        hidden=False,
        required_plans=PLAN_CORE,
        choices=["true", "false"]
    ),
    # Captcha settings
    "captcha-verification-enabled": Setting(
        name="captcha-verification-enabled",
        description="Require newcomers to pass a captcha challenge before they gain access.",
        setting_type=bool,
        default=False,
        choices=["true", "false"],
    ),
    "captcha-grace-period": Setting(
        name="captcha-grace-period",
        description="How long newcomers have to finish captcha verification (e.g. 10m, 1h).",
        setting_type=TimeString,
        default="10m",
    ),
    "captcha-success-actions": Setting(
        name="captcha-success-actions",
        description="Actions performed automatically after successful verification.",
        setting_type=list[str],
        default=[],
        hidden=True,
    ),
    "pre-captcha-roles": Setting(
        name="pre-captcha-roles",
        description="Roles assigned to newcomers before they complete captcha verification.",
        setting_type=list[discord.Role],
        default=[],
        hidden=True,
    ),
    "captcha-failure-actions": Setting(
        name="captcha-failure-actions",
        description="Actions to perform automatically when a member fails captcha verification.",
        setting_type=list[str],
        default=["kick"],
    ),
    "captcha-user-lookup": Setting(
        name="captcha-user-lookup",
        description="Check users against known violations during captcha verification.",
        setting_type=bool,
        default=False,
        required_plans=PLAN_PRO,
        choices=["true", "false"],
    ),
}


