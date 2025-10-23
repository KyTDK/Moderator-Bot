from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Tuple

__all__ = ["CaptchaSession", "CaptchaSessionStore"]


@dataclass(slots=True)
class CaptchaSession:
    """Represents a pending captcha verification session for a guild member."""

    guild_id: int
    user_id: int
    token: str | None
    expires_at: datetime | None
    state: str | None = None
    redirect: str | None = None
    delivery_method: str = "dm"
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        if now is None:
            now = datetime.now(timezone.utc)
        # Normalise to aware UTC for comparison safety.
        if self.expires_at.tzinfo is None:
            expires_at = self.expires_at.replace(tzinfo=timezone.utc)
        else:
            expires_at = self.expires_at.astimezone(timezone.utc)
        return now >= expires_at


class CaptchaSessionStore:
    """Thread-safe in-memory store for captcha sessions."""

    def __init__(self) -> None:
        self._sessions: Dict[Tuple[int, int], CaptchaSession] = {}
        self._lock = asyncio.Lock()

    async def put(self, session: CaptchaSession) -> None:
        key = (session.guild_id, session.user_id)
        async with self._lock:
            self._sessions[key] = session

    async def get(self, guild_id: int, user_id: int) -> CaptchaSession | None:
        key = (guild_id, user_id)
        async with self._lock:
            session = self._sessions.get(key)
            if session and session.is_expired():
                self._sessions.pop(key, None)
                return None
            return session
        
    async def peek(self, guild_id: int, user_id: int) -> CaptchaSession | None:
        key = (guild_id, user_id)
        async with self._lock:
            return self._sessions.get(key)

    async def remove(self, guild_id: int, user_id: int) -> None:
        key = (guild_id, user_id)
        async with self._lock:
            self._sessions.pop(key, None)

    async def consume(self, guild_id: int, user_id: int) -> CaptchaSession | None:
        """Return and remove the session if it exists and is valid."""

        key = (guild_id, user_id)
        async with self._lock:
            session = self._sessions.get(key)
            if session and session.is_expired():
                self._sessions.pop(key, None)
                return None
            if session:
                self._sessions.pop(key, None)
            return session
