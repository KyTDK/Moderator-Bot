from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import discord

from modules.captcha.sessions import CaptchaSession


@dataclass(slots=True)
class _PartialMember:
    guild: discord.Guild
    id: int

    @property
    def mention(self) -> str:
        return f"<@{self.id}>"


@dataclass(slots=True)
class _CaptchaProcessingContext:
    guild: discord.Guild
    member: discord.Member | _PartialMember
    settings: dict[str, Any]
    session: CaptchaSession


@dataclass(slots=True)
class _VpnPolicyContext:
    source: str
    decision: str
    actions: list[str] = field(default_factory=list)
    reason: str | None = None
    risk_score: float | None = None
    provider_count: int | None = None
    providers_flagged: int | None = None
    providers: list[dict[str, Any]] = field(default_factory=list)
    behavior: dict[str, Any] = field(default_factory=dict)
    hard_signals: list[str] = field(default_factory=list)
    cached_state: str | None = None
    escalation: str | None = None
    timestamp: int | None = None
    flagged_at: int | None = None
    requires_challenge: bool = False


__all__ = [
    "_PartialMember",
    "_CaptchaProcessingContext",
    "_VpnPolicyContext",
]
