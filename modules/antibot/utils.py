from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from collections import Counter
from math import log2



def age_days(dt: Optional[datetime]) -> Optional[int]:
    """Return approximate age in whole days for a datetime, or None."""
    if not dt:
        return None
    now = datetime.now(timezone.utc)
    base = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return max(0, int((now - base).total_seconds() // 86400))


def age_compact(dt: Optional[datetime]) -> str:
    """Return a compact age string like '3d' or '12h' (falls back to '?')."""
    if not dt:
        return "?"
    now = datetime.now(timezone.utc)
    base = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    secs = max(0, int((now - base).total_seconds()))
    if secs < 86400:
        hours = secs // 3600
        return f"{hours}h"
    days = secs // 86400
    return f"{days}d"


def fmt_bool(b: bool) -> str:
    return "Yes" if b else "No"


def shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = Counter(s)
    n = len(s)
    return -sum((c / n) * log2(c / n) for c in counts.values())


def digits_ratio(s: str) -> float:
    if not s:
        return 0.0
    d = sum(ch.isdigit() for ch in s)
    return d / len(s)


def longest_digit_run(s: str) -> int:
    best = cur = 0
    for ch in s:
        if ch.isdigit():
            cur += 1
            if cur > best:
                best = cur
        else:
            cur = 0
    return best
