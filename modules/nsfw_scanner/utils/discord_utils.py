"""Compatibility bridge for Discord helper utilities used by the NSFW scanner."""

from __future__ import annotations

from modules.utils.discord_utils import (
    ensure_member_with_presence,
    message_user,
    require_accelerated,
    resolve_role_references,
    safe_get_channel,
    safe_get_member,
    safe_get_message,
    safe_get_user,
)

__all__ = [
    "ensure_member_with_presence",
    "message_user",
    "require_accelerated",
    "resolve_role_references",
    "safe_get_channel",
    "safe_get_member",
    "safe_get_message",
    "safe_get_user",
]
