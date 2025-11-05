from __future__ import annotations

from modules.faq.constants import (
    DEFAULT_FAQ_SIMILARITY_THRESHOLD,
    MAX_FAQ_SIMILARITY_THRESHOLD,
    MIN_FAQ_SIMILARITY_THRESHOLD,
)
from modules.utils.localization import LocalizedError

from .base import Setting


def _validate_threshold(value) -> None:
    if value is None:
        return
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        raise LocalizedError(
            "modules.config.settings_schema.faq-threshold.invalid",
            "FAQ threshold must be between {min} and {max}.",
            placeholders={
                "min": MIN_FAQ_SIMILARITY_THRESHOLD,
                "max": MAX_FAQ_SIMILARITY_THRESHOLD,
            },
        ) from exc

    if not (MIN_FAQ_SIMILARITY_THRESHOLD <= numeric <= MAX_FAQ_SIMILARITY_THRESHOLD):
        raise LocalizedError(
            "modules.config.settings_schema.faq-threshold.invalid",
            "FAQ threshold must be between {min} and {max}.",
            placeholders={
                "min": MIN_FAQ_SIMILARITY_THRESHOLD,
                "max": MAX_FAQ_SIMILARITY_THRESHOLD,
            },
        )


def build_faq_settings() -> dict[str, Setting]:
    return {
        "faq-enabled": Setting(
            name="faq-enabled",
            description="Enable automatic FAQ responses for this server.",
            setting_type=bool,
            default=False,
            choices=["true", "false"],
            description_key="modules.config.settings_schema.faq-enabled.description",
        ),
        "faq-threshold": Setting(
            name="faq-threshold",
            description="Similarity threshold for FAQ matches (0.1 – 1.0).",
            setting_type=float,
            default=DEFAULT_FAQ_SIMILARITY_THRESHOLD,
            validator=_validate_threshold,
            description_key="modules.config.settings_schema.faq-threshold.description",
        ),
    }


__all__ = ["build_faq_settings"]
