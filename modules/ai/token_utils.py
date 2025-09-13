from math import ceil


def estimate_tokens(text: str) -> int:
    """Rough token estimate using ~1 token per 4 chars heuristic."""
    try:
        return ceil(len(text) / 4)
    except Exception:
        return 0

