from collections.abc import Iterable

__all__ = ["is_allowed_category"]


def is_allowed_category(category: str, allowed_categories: Iterable[str]) -> bool:
    """Check whether a category is present in the allowed_categories list."""
    normalized = category.replace("/", "_").replace("-", "_").lower()
    allowed = [item.lower() for item in allowed_categories]
    return normalized in allowed
