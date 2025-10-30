import os

__all__ = ["safe_delete"]


def safe_delete(path: str) -> None:
    """Remove a file if it exists, ignoring missing files."""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
