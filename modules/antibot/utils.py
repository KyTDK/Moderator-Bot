from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def age_days(dt: Optional[datetime]) -> Optional[int]:
    """Return approximate age in whole days for a datetime, or None."""
    if not dt:
        return None
    now = datetime.now(timezone.utc)
    base = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return max(0, int((now - base).total_seconds() // 86400))


def fmt_bool(b: bool) -> str:
    return "Yes" if b else "No"

