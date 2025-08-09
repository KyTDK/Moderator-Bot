import re
from datetime import timedelta

def parse_duration(duration_str):
    if duration_str is None:
        return None

    try:
        duration_str = str(duration_str).strip()
    except Exception:
        return None

    pattern = r"(\d+)\s*(s|sec|second|seconds|m|min|minute|minutes|h|hr|hour|hours|d|day|days|w|week|weeks|mo|month|months|y|year|years)"
    match = re.fullmatch(pattern, duration_str, flags=re.IGNORECASE)
    if not match:
        return None

    value, unit = match.groups()
    value = int(value)
    unit = unit.lower()

    if unit in ("s", "sec", "second", "seconds"):
        return timedelta(seconds=value)
    elif unit in ("m", "min", "minute", "minutes"):
        return timedelta(minutes=value)
    elif unit in ("h", "hr", "hour", "hours"):
        return timedelta(hours=value)
    elif unit in ("d", "day", "days"):
        return timedelta(days=value)
    elif unit in ("w", "week", "weeks"):
        return timedelta(weeks=value)
    elif unit in ("mo", "month", "months"):
        return timedelta(days=value * 30)
    elif unit in ("y", "year", "years"):
        return timedelta(days=value * 365)
    else:
        return None