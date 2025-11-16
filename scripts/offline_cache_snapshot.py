#!/usr/bin/env python3
"""One-shot utility to mirror the current MySQL state into the offline cache."""

from __future__ import annotations

import argparse
import asyncio
import logging

from modules.utils import mysql
from modules.utils.mysql.connection import refresh_offline_cache_snapshot


async def _run(verbose: bool) -> None:
    if verbose:
        logging.basicConfig(level=logging.INFO)

    await mysql.initialise_and_get_pool()
    await refresh_offline_cache_snapshot()
    await mysql.close_pool()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mirror the live MySQL database into the offline cache.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Emits INFO logs while the snapshot is running.",
    )
    args = parser.parse_args()
    asyncio.run(_run(args.verbose))


if __name__ == "__main__":
    main()
