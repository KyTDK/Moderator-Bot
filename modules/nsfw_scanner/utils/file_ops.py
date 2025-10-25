import base64
import os

__all__ = ["safe_delete", "file_to_b64"]


def safe_delete(path: str) -> None:
    """Remove a file if it exists, ignoring missing files."""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def file_to_b64(path: str) -> str:
    """Return a base64-encoded representation of the file contents."""
    with open(path, "rb") as file_obj:
        return base64.b64encode(file_obj.read()).decode()
