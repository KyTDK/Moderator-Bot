from __future__ import annotations

import logging
import warnings

NOISY_LIBRARIES = (
    "discord",
    "aiomysql",
    "aiohttp",
    "pymilvus",
    "transformers",
    "urllib3",
)


def configure_logging(level_name: str) -> logging.Logger:
    level = getattr(logging, level_name, logging.WARNING)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    for lib in NOISY_LIBRARIES:
        logging.getLogger(lib).setLevel(level)

    warnings.filterwarnings(
        "ignore",
        message=r".*(database|Table).*already exists.*",
        category=Warning,
    )

    return logging.getLogger(__name__)
