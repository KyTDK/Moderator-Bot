import re
from datetime import timedelta

def parse_duration(duration_str):
    # Strip any leading/trailing whitespace
    if duration_str is None:
        return None
    duration_str = duration_str.strip()
    
    # Match digits followed by time units (s, m, h, d, w, mo, y)
    match = re.match(r"(\d+)([smhdwmy])", duration_str)
    if not match:
        return None  # Return None if the format is incorrect
    
    value, unit = match.groups()
    value = int(value)
    
    if unit == "s":
        return timedelta(seconds=value)
    elif unit == "m":
        return timedelta(minutes=value)
    elif unit == "h":
        return timedelta(hours=value)
    elif unit == "d":
        return timedelta(days=value)
    elif unit == "w":  # weeks
        return timedelta(weeks=value)
    elif unit == "mo":  # months (approx. 30 days)
        return timedelta(days=value * 30)  # This is an approximation
    elif unit == "y":  # years (approx. 365 days)
        return timedelta(days=value * 365)  # Approximate year length
    else:
        return None  # Return None for unrecognized unit
