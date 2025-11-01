from __future__ import annotations

from .base import Setting
from .general import build_general_settings
from .nsfw import build_nsfw_settings


def _build_settings_schema() -> dict[str, Setting]:
    settings: dict[str, Setting] = {}
    settings.update(build_general_settings())
    settings.update(build_nsfw_settings())
    return settings


SETTINGS_SCHEMA = _build_settings_schema()

__all__ = ["Setting", "SETTINGS_SCHEMA"]
