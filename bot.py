from __future__ import annotations

import asyncio
import logging
import time

from modules.core import configure_logging, load_runtime_config
from modules.core.moderator_bot import ModeratorBot
from modules.utils import mysql

print(f"[BOOT] Starting Moderator Bot at {time.strftime('%X')}")

async def _main() -> None:
    config = load_runtime_config()
    configure_logging(config.log_level)
    logger = logging.getLogger(__name__)

    if not config.token:
        print("[FATAL] DISCORD_TOKEN is not set. Exiting.")
        return

    shard_assignment: mysql.ShardAssignment | None = None
    bot: ModeratorBot | None = None

    try:
        await mysql.initialise_and_get_pool()
        try:
            shard_assignment = await mysql.claim_shard(
                config.shard.instance_id,
                total_shards=config.shard.total_shards,
                preferred_shard=config.shard.preferred_shard,
                stale_after_seconds=config.shard.stale_seconds,
            )
        except mysql.ShardClaimError as exc:
            logger.error("Shard claim failed: %s", exc)
            return

        print(
            f"[SHARD] Instance {config.shard.instance_id} claimed shard "
            f"{shard_assignment.shard_id}/{shard_assignment.shard_count}"
        )

        bot = ModeratorBot(
            shard_assignment,
            instance_id=config.shard.instance_id,
            heartbeat_seconds=config.shard.heartbeat_seconds,
            log_cog_loads=config.log_cog_loads,
        )

        await bot.start(config.token)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logger.exception("[FATAL] Bot crashed: %s", exc)
        if shard_assignment is not None:
            try:
                await mysql.update_shard_status(
                    shard_id=shard_assignment.shard_id,
                    instance_id=config.shard.instance_id,
                    status="error",
                    last_error=str(exc),
                )
            except Exception:
                logger.exception("Failed to persist shard error state")
    finally:
        if bot is not None and not bot.is_closed():
            try:
                await bot.close()
            except Exception:
                logger.exception("Failed to close bot cleanly")
        if shard_assignment is not None:
            try:
                await mysql.release_shard(shard_assignment.shard_id, config.shard.instance_id)
            except Exception:
                logger.exception(
                    "Failed to release shard %s", shard_assignment.shard_id
                )
        try:
            await mysql.close_pool()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(_main())

