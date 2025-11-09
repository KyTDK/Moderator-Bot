from __future__ import annotations

import os
from typing import Tuple

from dotenv import load_dotenv

load_dotenv()

DEV_GUILD_ID: int = int(os.getenv("GUILD_ID", "0"))
ALLOWED_USER_IDS: Tuple[int, ...] = tuple(
    int(entry)
    for entry in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if entry.strip().isdigit()
)

__all__ = ["DEV_GUILD_ID", "ALLOWED_USER_IDS"]
