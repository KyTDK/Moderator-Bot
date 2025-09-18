from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import discord

def resolve_user(member: discord.Member) -> discord.abc.User | discord.Member:
    """Return the underlying user object for a member when available."""
    try:
        return member._user if hasattr(member, "_user") else member
    except Exception:
        return member

@dataclass
class ScoreContext:
    member: discord.Member
    user: discord.abc.User | discord.Member
    bot: Optional[discord.Client] = None
    score: int = 0
    details: Dict[str, Any] = field(default_factory=dict)
    contributions: Dict[str, int] = field(default_factory=dict)

    def add(self, label: str, value: int) -> None:
        if not value:
            return
        self.score += value
        self.contributions[label] = self.contributions.get(label, 0) + value

    def set_detail(self, key: str, value: Any) -> None:
        self.details[key] = value

    def extend_details(self, extra: Dict[str, Any]) -> None:
        if extra:
            self.details.update(extra)

    def clamp(self, minimum: int, maximum: int) -> None:
        if self.score < minimum:
            self.score = minimum
        elif self.score > maximum:
            self.score = maximum

    def result_details(self) -> Dict[str, Any]:
        return {
            **self.details,
            "final_score": self.score,
            "contrib": self.contributions,
        }
