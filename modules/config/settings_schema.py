from typing import Any, Callable, Optional
from collections.abc import Iterable
import json
from pathlib import Path

import discord
from modules.config.premium_plans import PLAN_CORE, PLAN_PRO, PLAN_ULTRA, resolve_required_plans
from modules.i18n.locale_utils import list_supported_locales, normalise_locale
from modules.utils.localization import LocalizedError
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
        description_key: str | None = None,
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
        self.description_key = description_key

        normalized_plans = resolve_required_plans(required_plans) if required_plans is not None else None
        self.required_plans = frozenset(normalized_plans) if normalized_plans else None
        self.accelerated = bool(self.required_plans)

    async def validate(self, value: Any):
        if self.validator:
            await self.validator(value)


async def _validate_locale(value: Any) -> None:
    if value is None:
        return

    normalized = normalise_locale(value)
    raw_value = getattr(value, "value", value)
    candidate = str(raw_value).strip().replace("_", "-") if raw_value is not None else ""

    if not normalized or candidate.lower() != normalized.lower():
        supported = ", ".join(list_supported_locales())
        raise LocalizedError(
            "modules.config.settings_schema.locale.invalid",
            "Invalid locale. Supported locales: {supported}",
            placeholders={"supported": supported},
        )

def _load_en_settings_schema_locale() -> dict[str, Any]:
    locale_path = (
        Path(__file__).resolve().parents[2]
        / "locales"
        / "en"
        / "modules.config.settings_schema.json"
    )
    try:
        with locale_path.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
    except FileNotFoundError as exc:  # pragma: no cover - defensive
        raise RuntimeError(
            "Missing English locale file for settings schema defaults"
        ) from exc

    settings_data = data.get("modules", {}).get("config", {}).get("settings_schema", {})
    if not settings_data:
        raise RuntimeError(
            "Settings schema locale defaults are missing from locales/en/modules.config.settings_schema.json"
        )
    return settings_data


def _get_locale_value(data: dict[str, Any], path: list[str]) -> Any:
    current: Any = data
    for part in path:
        if not isinstance(current, dict) or part not in current:
            raise RuntimeError(
                "Missing locale value for settings schema path: " + ".".join(path)
            )
        current = current[part]
    return current


_LOCALE_DEFAULTS = _load_en_settings_schema_locale()

_NSFW_PFP_DEFAULT_MESSAGE = _get_locale_value(
    _LOCALE_DEFAULTS, ["nsfw_pfp_message", "default"]
)
_NSFW_PFP_DEFAULT_CHOICES = _get_locale_value(
    _LOCALE_DEFAULTS, ["nsfw_pfp_message", "choices"]
)

_RULES_DEFAULT_TEXT = _get_locale_value(_LOCALE_DEFAULTS, ["rules", "default"])
_VCMOD_RULES_DEFAULT_TEXT = _get_locale_value(
    _LOCALE_DEFAULTS, ["vcmod_rules", "default"]
)

if not isinstance(_NSFW_PFP_DEFAULT_MESSAGE, str):
    raise RuntimeError("NSFW profile picture default message must be a string")
if not isinstance(_NSFW_PFP_DEFAULT_CHOICES, list):
    raise RuntimeError("NSFW profile picture message choices must be a list")

if isinstance(_RULES_DEFAULT_TEXT, list):
    _RULES_DEFAULT_TEXT = "\n".join(str(part) for part in _RULES_DEFAULT_TEXT)
elif not isinstance(_RULES_DEFAULT_TEXT, str):
    raise RuntimeError("Rules default text must resolve to a string")

if isinstance(_VCMOD_RULES_DEFAULT_TEXT, list):
    _VCMOD_RULES_DEFAULT_TEXT = "\n".join(
        str(part) for part in _VCMOD_RULES_DEFAULT_TEXT
    )
elif not isinstance(_VCMOD_RULES_DEFAULT_TEXT, str):
    raise RuntimeError("VC moderation rules default must resolve to a string")

SETTINGS_SCHEMA = {
    "strike-channel": Setting(
        name="strike-channel",
        description="Channel where strikes are logged.",
        setting_type=discord.TextChannel,
        hidden=True,
        description_key="modules.config.settings_schema.strike-channel.description",
    ),
    "aimod-debug": Setting(
        name="aimod-debug",
        description="Always send a detailed AI moderation debug message to the AI violations channel, including flagged messages, AI decision, and applied actions.",
        setting_type=bool,
        default=False,
        choices=["true", "false"],
        description_key="modules.config.settings_schema.aimod-debug.description",
    ),
    "nsfw-verbose": Setting(
        name="nsfw-verbose",
        description="Post a detailed scan report embed in the same channel as the scanned media.",
        setting_type=bool,
        default=False,
        choices=["true", "false"],
        description_key="modules.config.settings_schema.nsfw-verbose.description",
    ),
    "nsfw-channel": Setting(
        name="nsfw-channel",
        description="Channel where NSFW violations are logged with a preview of the media.",
        setting_type=discord.TextChannel,
        hidden=True,
        description_key="modules.config.settings_schema.nsfw-channel.description",
    ),
    "nsfw-channel-notify": Setting(
        name="nsfw-channel-notify",
        description="Send a notification embed to the channel when NSFW content is removed.",
        setting_type=bool,
        default=True,
        choices=["true", "false"],
        description_key="modules.config.settings_schema.nsfw-channel-notify.description",
    ),
    "nsfw-enabled": Setting(
        name="nsfw-enabled",
        description="Enable NSFW scanning for messages, reactions, and avatars (other toggles still apply).",
        setting_type=bool,
        default=True,
        choices=["true", "false"],
        description_key="modules.config.settings_schema.nsfw-enabled.description",
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
        hidden=True,
        description_key="modules.config.settings_schema.monitor-events.description",
    ),
    "locale": Setting(
        name="locale",
        description="Override automatic locale detection with a specific locale code.",
        setting_type=str,
        default=None,
        validator=_validate_locale,
        choices=list_supported_locales(),
        description_key="modules.config.settings_schema.locale.description",
    ),
    "nsfw-detection-action": Setting(
        name="nsfw-detection-action",
        description="Action to take when a user posts NSFW content.",
        setting_type=list[str],
        default=["delete", "strike"],
        hidden=True,
        choices=["strike", "kick", "ban", "timeout", "delete"],
        description_key="modules.config.settings_schema.nsfw-detection-action.description",
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
        description_key="modules.config.settings_schema.nsfw-detection-categories.description",
    ),
    "nsfw-high-accuracy": Setting(
        name="nsfw-high-accuracy",
        description="Enable high-accuracy NSFW scans for more reliable detection. Accelerated only.",
        setting_type=bool,
        default=False,
        required_plans=PLAN_CORE,
        choices=["true", "false"],
        description_key="modules.config.settings_schema.nsfw-high-accuracy.description",
    ),
    "threshold": Setting(
        name="threshold",
        description="Threshold for NSFW detection confidence. Lower values are more sensitive.",
        setting_type=float,
        default=0.70,
        hidden=True,
        description_key="modules.config.settings_schema.threshold.description",
    ),
    "banned-words-action": Setting(
        name="banned-words-action",
        description="Action to take when a user posts a banned word.",
        setting_type=list[str],
        default=["delete"],
        hidden=True,
        choices=["strike", "kick", "ban", "timeout", "delete"],
        description_key="modules.config.settings_schema.banned-words-action.description",
    ),
    "monitor-channel": Setting(
        name="monitor-channel",
        hidden=True,
        description="Channel to log all server activities, including message edits, deletions, and user join/leave events.",
        setting_type=discord.TextChannel,
        description_key="modules.config.settings_schema.monitor-channel.description",
    ),
    "captcha-log-channel": Setting(
        name="captcha-log-channel",
        description="Channel where captcha verification logs and activity updates are posted.",
        setting_type=discord.TextChannel,
        hidden=True,
        description_key="modules.config.settings_schema.captcha-log-channel.description",
    ),
    "aimod-channel": Setting(
        name="aimod-channel",
        description="Channel where AI violation logs are posted.",
        setting_type=discord.TextChannel,
        hidden=True,
        description_key="modules.config.settings_schema.aimod-channel.description",
    ),
    "cycle-strike-actions": Setting(
        name="cycle-strike-actions",
        description="Cycle through strike actions when run out of actions to give user.",
        setting_type=bool,
        default=True,
        choices=["true", "false"],
        description_key="modules.config.settings_schema.cycle-strike-actions.description",
    ),
    "exclude-bannedwords-channels": Setting(
        name="exclude-bannedwords-channels",
        description="Channels to exclude from banned words detection.",
        setting_type=list[discord.TextChannel],
        default=[],
        description_key="modules.config.settings_schema.exclude-bannedwords-channels.description",
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
        description_key="modules.config.settings_schema.strike-actions.description",
    ),
    "strike-expiry": Setting(
        name="strike-expiry",
        description="Time a strike lasts for. Use formats like 20s, 30m, 2h, 30d, 2w, 5mo, 1y. Seconds, minutes, hours, days, weeks, months and years respectively.",
        setting_type=TimeString,
        default=None,
        description_key="modules.config.settings_schema.strike-expiry.description",
    ),
    "dm-on-strike": Setting(
        name="dm-on-strike",
        description="DM the user when they receive a strike.",
        setting_type=bool,
        default=True,
        choices=["true", "false"],
        description_key="modules.config.settings_schema.dm-on-strike.description",
    ),
    "check-pfp": Setting(
        name="check-pfp",
        description="Check the profile picture of the user for NSFW content.",
        setting_type=bool,
        default=False, # False by default to avoid unnecessary API calls
        choices=["true", "false"],
        required_plans=PLAN_CORE,
        description_key="modules.config.settings_schema.check-pfp.description",
    ),
    "nsfw-pfp-action": Setting(
        name="nsfw-pfp-action",
        description="Action to take when a user sets an NSFW profile picture.",
        setting_type=list[str],
        default=["kick"],
        choices=["strike", "strike:2d", "kick", "ban", "timeout:1d", "timeout:7d"],
        description_key="modules.config.settings_schema.nsfw-pfp-action.description",
    ),
    "nsfw-pfp-message": Setting(
        name="nsfw-pfp-message",
        description="The message to send when a player has had their profile picture flagged.",
        setting_type=str,
        default=_NSFW_PFP_DEFAULT_MESSAGE,
        choices=list(_NSFW_PFP_DEFAULT_CHOICES),
        required_plans=PLAN_CORE,
        description_key="modules.config.settings_schema.nsfw-pfp-message.description",
    ),
    "unmute-on-safe-pfp": Setting(
        name="unmute-on-safe-pfp",
        description="Remove timeout of a user once they have changed their profile picture to something appropriate.",
        setting_type=bool,
        default=False,
        choices=["true", "false"],
        required_plans=PLAN_CORE,
        description_key="modules.config.settings_schema.unmute-on-safe-pfp.description",
    ),
    "scan-age-restricted": Setting(
        name="scan-age-restricted",
        description="Scan age-restricted (NSFW) channels. When off, NSFW channels are skipped.",
        setting_type=bool,
        default=True,
        choices=["true", "false"],
        description_key="modules.config.settings_schema.scan-age-restricted.description",
    ),
    "check-tenor-gifs": Setting(
        name="check-tenor-gifs",
        description="Apply punishments when NSFW content is detected in Tenor GIFs.",
        setting_type=bool,
        default=False,
        choices=["true", "false"],
        description_key="modules.config.settings_schema.check-tenor-gifs.description",
    ),
    "use-default-banned-words": Setting(
        name="use-default-banned-words",
        description="Use the built-in profanity list for this server.",
        setting_type=bool,
        default=False,
        hidden=True,
        choices=["true", "false"],
        description_key="modules.config.settings_schema.use-default-banned-words.description",
    ),
    "exclude-channels": Setting(
        name="exclude-channels",
        description="Channels to exclude from detection.",
        setting_type=list[discord.TextChannel],
        default=[],
        description_key="modules.config.settings_schema.exclude-channels.description",
    ),
    "delete-scam-messages": Setting(
        name="delete-scam-messages",
        description="Automatically delete messages that match scam patterns or URLs.",
        setting_type=bool,
        default=True,
        hidden=True,
        choices=["true", "false"],
        description_key="modules.config.settings_schema.delete-scam-messages.description",
    ),
    "scam-detection-action": Setting(
        name="scam-detection-action",
        description="Action to take when a scam message is detected.",
        setting_type=list[str],
        hidden=True,
        default=["delete"],
        choices=["strike", "kick", "ban", "timeout", "delete"],
        description_key="modules.config.settings_schema.scam-detection-action.description",
    ),
    "check-links": Setting(
        name="check-links",
        description="Check links in messages for malware, phishing, scamming etc.",
        setting_type=bool,
        default=False,
        hidden=True,
        choices=["true", "false"],
        description_key="modules.config.settings_schema.check-links.description",
    ),
    "exclude-scam-channels": Setting(
        name="exclude-scam-channels",
        description="Channels to exclude from scam detection.",
        setting_type=list[discord.TextChannel],
        default=[],
        hidden=True,
        description_key="modules.config.settings_schema.exclude-scam-channels.description",
    ),
    "rules": Setting(
        name="rules",
        description="The server rules used for autonomous moderation.",
        setting_type=str,
        default=_RULES_DEFAULT_TEXT,
        hidden=True,
        required_plans=PLAN_CORE,
        description_key="modules.config.settings_schema.rules.description",
    ),
    "aimod-detection-action": Setting(
        name="aimod-detection-action",
        description="Action to take when the autonomous moderator detects a violation. Use auto for the AI to make the decision.",
        setting_type=list[str],
        hidden=True,
        default=["auto"],
        required_plans=PLAN_CORE,
        choices=["strike", "kick", "ban", "timeout", "delete", "auto"],
        description_key="modules.config.settings_schema.aimod-detection-action.description",
    ),
    "aimod-high-accuracy": Setting(
        name="aimod-high-accuracy",
        description=(
            "Use higher-accuracy AI moderation with gpt-5-mini (approx. 2.25 USD per 1M tokens). "
            "Consumes the monthly budget faster compared to the default."
        ),
        setting_type=bool,
        default=False,
        required_plans=PLAN_PRO,
        choices=["true", "false"],
        description_key="modules.config.settings_schema.aimod-high-accuracy.description",
    ),

    "autonomous-mod": Setting(
        name="autonomous-mod",
        description="Use AI to automatically moderate your entire server.",
        setting_type=bool,
        default=False,
        hidden=True,
        required_plans=PLAN_CORE,
        choices=["true", "false"],
        description_key="modules.config.settings_schema.autonomous-mod.description",
    ),
    "aimod-check-interval": Setting(
        name="aimod-check-interval",
        description="How often to run the AI moderation batch process.",
        setting_type=TimeString,
        required_plans=PLAN_CORE,
        default=TimeString("1h"),
        description_key="modules.config.settings_schema.aimod-check-interval.description",
    ),
    "aimod-mode": Setting(
        name="aimod-mode",
        description="Choose how AI moderation is triggered: `report` mode (only on mention), `interval` mode (automatic background scanning), or `adaptive` mode (dynamically switches based on server activity, events, or configuration).",
        setting_type=str,
        default="report",
        required_plans=PLAN_CORE,
        choices=["interval", "report", "adaptive"],
        hidden=True,
        description_key="modules.config.settings_schema.aimod-mode.description",
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
        hidden=True,
        description_key="modules.config.settings_schema.aimod-adaptive-events.description",
    ),
    "no-forward-from-role": Setting(
        name="no-forward-from-role",
        description="Stop a specific role from forwarding messages.",
        setting_type=list[discord.Role],
        default=[],
        description_key="modules.config.settings_schema.no-forward-from-role.description",
    ),
    "banned-urls": Setting(
        name="blocked-urls",
        description="Exact URLs or domains to block.",
        setting_type=list[str],
        default=[],
        description_key="modules.config.settings_schema.banned-urls.description",
    ),
    "url-detection-action": Setting(
        name="url-detection-action",
        description="Action to take when a blocked URL is detected.",
        setting_type=list[str],
        default=["delete"],
        choices=["delete", "strike", "kick", "ban", "timeout"],
        description_key="modules.config.settings_schema.url-detection-action.description",
    ),
    "exclude-url-channels": Setting(
        name="exclude-url-channels",
        description="Channels to exclude from banned URLs.",
        setting_type=list[discord.TextChannel],
        default=[],
        description_key="modules.config.settings_schema.exclude-url-channels.description",
    ),
    "vcmod-enabled": Setting(
        name="vcmod-enabled",
        description="Enable voice channel moderation and transcription.",
        setting_type=bool,
        default=False,
        hidden=True,
        required_plans=PLAN_CORE,
        choices=["true", "false"],
        description_key="modules.config.settings_schema.vcmod-enabled.description",
    ),
    "vcmod-channels": Setting(
        name="vcmod-channels",
        description="Voice channels to monitor (when empty, none are monitored).",
        setting_type=list[discord.VoiceChannel],
        default=[],
        hidden=True,
        description_key="modules.config.settings_schema.vcmod-channels.description",
    ),
    "vcmod-listen-duration": Setting(
        name="vcmod-listen-duration",
        description="How long to actively listen per voice channel during a cycle (e.g., 2m, 5m).",
        setting_type=TimeString,
        default=TimeString("2m"),
        required_plans=PLAN_CORE,
        hidden=True,
        description_key="modules.config.settings_schema.vcmod-listen-duration.description",
    ),
    "vcmod-idle-duration": Setting(
        name="vcmod-idle-duration",
        description="How long to pause between each transcription in Saver Mode.",
        setting_type=TimeString,
        default=TimeString("30s"),
        required_plans=PLAN_CORE,
        hidden=True,
        description_key="modules.config.settings_schema.vcmod-idle-duration.description",
    ),
    "vcmod-saver-mode": Setting(
        name="vcmod-saver-mode",
        description="Saver mode: bot cycles through VCs to appear active but does not record/listen most of the time.",
        setting_type=bool,
        default=False,
        hidden=True,
        required_plans=PLAN_CORE,
        choices=["true", "false"],
        description_key="modules.config.settings_schema.vcmod-saver-mode.description",
    ),
    "vcmod-rules": Setting(
        name="vcmod-rules",
        description="Rules applied to voice chat moderation (separate from text rules).",
        setting_type=str,
        default=_VCMOD_RULES_DEFAULT_TEXT,
        hidden=True,
        required_plans=PLAN_CORE,
        description_key="modules.config.settings_schema.vcmod-rules.description",
    ),
    "vcmod-detection-action": Setting(
        name="vcmod-detection-action",
        description="Action to take when VC moderation detects a violation. Use auto for the AI to decide.",
        setting_type=list[str],
        default=["auto"],
        hidden=True,
        required_plans=PLAN_CORE,
        choices=["strike", "kick", "ban", "timeout", "delete", "auto"],
        description_key="modules.config.settings_schema.vcmod-detection-action.description",
    ),
    "vcmod-high-accuracy": Setting(
        name="vcmod-high-accuracy",
        description="Use higher-accuracy AI analysis for VC moderation (uses more token budget).",
        setting_type=bool,
        default=False,
        hidden=True,
        required_plans=PLAN_PRO,
        choices=["true", "false"],
        description_key="modules.config.settings_schema.vcmod-high-accuracy.description",
    ),
    "vcmod-high-quality-transcription": Setting(
        name="vcmod-high-quality-transcription",
        description="Enable clearer voice transcripts with premium processing (higher cost).",
        setting_type=bool,
        default=False,
        hidden=True,
        required_plans=PLAN_PRO,
        choices=["true", "false"],
        description_key="modules.config.settings_schema.vcmod-high-quality-transcription.description",
    ),
    "vcmod-transcript-channel": Setting(
        name="vcmod-transcript-channel",
        description="Channel where VC transcripts are posted.",
        setting_type=discord.TextChannel,
        hidden=True,
        description_key="modules.config.settings_schema.vcmod-transcript-channel.description",
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
        choices=["true", "false"],
        description_key="modules.config.settings_schema.vcmod-transcript-only.description",
    ),
    # Captcha settings
    "captcha-verification-enabled": Setting(
        name="captcha-verification-enabled",
        description="Require newcomers to pass a captcha challenge before they gain access.",
        setting_type=bool,
        default=False,
        choices=["true", "false"],
        description_key="modules.config.settings_schema.captcha-verification-enabled.description",
    ),
    "captcha-grace-period": Setting(
        name="captcha-grace-period",
        description="How long newcomers have to finish captcha verification (e.g. 10m, 1h).",
        setting_type=TimeString,
        default="0", # No time limit by default
        description_key="modules.config.settings_schema.captcha-grace-period.description",
    ),
    "captcha-success-actions": Setting(
        name="captcha-success-actions",
        description="Actions performed automatically after successful verification.",
        setting_type=list[str],
        default=[],
        hidden=True,
        description_key="modules.config.settings_schema.captcha-success-actions.description",
    ),
    "captcha-delivery-method": Setting(
        name="captcha-delivery-method",
        description="How captcha verification links are delivered to new members (dm or embed).",
        setting_type=str,
        default="dm",
        choices=["dm", "embed"],
        hidden=True,
        description_key="modules.config.settings_schema.captcha-delivery-method.description",
    ),
    "captcha-embed-channel-id": Setting(
        name="captcha-embed-channel-id",
        description="Channel where the captcha verification embed is posted when using embed delivery.",
        setting_type=discord.TextChannel,
        hidden=True,
        description_key="modules.config.settings_schema.captcha-embed-channel-id.description",
    ),
    "pre-captcha-roles": Setting(
        name="pre-captcha-roles",
        description="Roles assigned to newcomers before they complete captcha verification.",
        setting_type=list[discord.Role],
        default=[],
        hidden=True,
        description_key="modules.config.settings_schema.pre-captcha-roles.description",
    ),
    "captcha-failure-actions": Setting(
        name="captcha-failure-actions",
        description="Actions to perform automatically when a member fails captcha verification.",
        setting_type=list[str],
        default=["kick"],
        description_key="modules.config.settings_schema.captcha-failure-actions.description",
    ),
    "vpn-detection-enabled": Setting(
        name="vpn-detection-enabled",
        description="Enable VPN and proxy screening during verification.",
        setting_type=bool,
        default=False,
        hidden=True,
        description_key="modules.config.settings_schema.vpn-detection-enabled.description",
    ),
    "vpn-detection-actions": Setting(
        name="vpn-detection-actions",
        description="Actions to perform when VPN detection denies access.",
        setting_type=list[str],
        default=["kick"],
        hidden=True,
        description_key="modules.config.settings_schema.vpn-detection-actions.description",
    ),
    "vpn-pre-actions": Setting(
        name="vpn-pre-actions",
        description="Role adjustments applied before VPN screening.",
        setting_type=list[str],
        default=[],
        hidden=True,
        description_key="modules.config.settings_schema.vpn-pre-actions.description",
    ),
    "vpn-post-actions": Setting(
        name="vpn-post-actions",
        description="Role adjustments applied after successful VPN screening.",
        setting_type=list[str],
        default=[],
        hidden=True,
        description_key="modules.config.settings_schema.vpn-post-actions.description",
    ),
}

