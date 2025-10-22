from typing import Any

__all__ = ["NSFWScanner", "handle_nsfw_content"]


def __getattr__(name: str) -> Any:
    if name == "NSFWScanner":
        from .scanner import NSFWScanner

        return NSFWScanner
    if name == "handle_nsfw_content":
        from .actions import handle_nsfw_content

        return handle_nsfw_content
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
