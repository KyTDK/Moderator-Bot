from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from modules.config.premium_plans import resolve_required_plans
from modules.i18n.locale_utils import list_supported_locales, normalise_locale
from modules.utils.localization import LocalizedError

__all__ = [
    "Setting",
    "load_locale_defaults",
    "get_locale_value",
    "validate_locale_setting",
    "LOCALE_DEFAULTS",
    "SUPPORTED_LOCALES",
    "NSFW_PFP_DEFAULT_MESSAGE",
    "NSFW_PFP_DEFAULT_CHOICES",
    "RULES_DEFAULT_TEXT",
    "VCMOD_RULES_DEFAULT_TEXT",
]


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
        validator: Optional[Callable[[Any], Any]] = None,
        choices: Optional[list[str]] = None,
        required_plans: str | Iterable[str] | None = None,
        description_key: str | None = None,
    ) -> None:
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

        normalized_plans = (
            resolve_required_plans(required_plans)
            if required_plans is not None
            else None
        )
        self.required_plans = frozenset(normalized_plans) if normalized_plans else None
        self.accelerated = bool(self.required_plans)

    async def validate(self, value: Any) -> None:
        if self.validator:
            await self.validator(value)


async def validate_locale_setting(value: Any) -> None:
    if value is None:
        return

    normalized = normalise_locale(value)
    raw_value = getattr(value, "value", value)
    candidate = str(raw_value).strip().replace("_", "-") if raw_value is not None else ""

    if not normalized or candidate.lower() != normalized.lower():
        supported = ", ".join(list_supported_locales())
        raise LocalizedError(
            "modules.config.settings_schema.locale.invalid",
            "Invalid locale. Supported locales: {supported}.",
            placeholders={"supported": supported},
        )


def load_locale_defaults() -> dict[str, Any]:
    locale_path = (
        Path(__file__).resolve().parents[3]
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


def get_locale_value(data: dict[str, Any], path: list[str]) -> Any:
    current: Any = data
    for part in path:
        if not isinstance(current, dict) or part not in current:
            raise RuntimeError(
                "Missing locale value for settings schema path: " + ".".join(path)
            )
        current = current[part]
    return current


LOCALE_DEFAULTS = load_locale_defaults()
SUPPORTED_LOCALES = list_supported_locales()

NSFW_PFP_DEFAULT_MESSAGE = get_locale_value(
    LOCALE_DEFAULTS, ["nsfw_pfp_message", "default"]
)
NSFW_PFP_DEFAULT_CHOICES = get_locale_value(
    LOCALE_DEFAULTS, ["nsfw_pfp_message", "choices"]
)

RULES_DEFAULT_TEXT = get_locale_value(LOCALE_DEFAULTS, ["rules", "default"])
VCMOD_RULES_DEFAULT_TEXT = get_locale_value(
    LOCALE_DEFAULTS, ["vcmod_rules", "default"]
)

if not isinstance(NSFW_PFP_DEFAULT_MESSAGE, str):
    raise RuntimeError("NSFW profile picture default message must be a string")
if not isinstance(NSFW_PFP_DEFAULT_CHOICES, list):
    raise RuntimeError("NSFW profile picture message choices must be a list")

if isinstance(RULES_DEFAULT_TEXT, list):
    RULES_DEFAULT_TEXT = "\n".join(str(part) for part in RULES_DEFAULT_TEXT)
elif not isinstance(RULES_DEFAULT_TEXT, str):
    raise RuntimeError("Rules default text must resolve to a string")

if isinstance(VCMOD_RULES_DEFAULT_TEXT, list):
    VCMOD_RULES_DEFAULT_TEXT = "\n".join(str(part) for part in VCMOD_RULES_DEFAULT_TEXT)
elif not isinstance(VCMOD_RULES_DEFAULT_TEXT, str):
    raise RuntimeError("VC moderation rules default must resolve to a string")
