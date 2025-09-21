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
    standby_prepared = False
    bot = ModeratorBot(
        instance_id=config.shard.instance_id,
        heartbeat_seconds=config.shard.heartbeat_seconds,
        instance_heartbeat_seconds=config.shard.instance_heartbeat_seconds,
        log_cog_loads=config.log_cog_loads,
        total_shards=config.shard.total_shards,
    )

    try:
        await mysql.initialise_and_get_pool()
        failover_after_seconds = max(config.shard.heartbeat_seconds * 2, 30)
        stale_after_seconds = max(
            30,
            min(config.shard.stale_seconds, failover_after_seconds),
        )
        instance_failover_after_seconds = max(
            config.shard.instance_heartbeat_seconds * 2,
            config.shard.instance_heartbeat_seconds + 3,
            6,
        )
        await mysql.recover_stuck_shards(
            stale_after_seconds,
            instance_stale_after_seconds=instance_failover_after_seconds,
        )
        while True:
            try:
                await mysql.update_instance_heartbeat(config.shard.instance_id)
                shard_assignment = await mysql.claim_shard(
                    config.shard.instance_id,
                    total_shards=config.shard.total_shards,
                    preferred_shard=config.shard.preferred_shard,
                    stale_after_seconds=stale_after_seconds,
                    instance_stale_after_seconds=instance_failover_after_seconds,
                )
                break
            except mysql.ShardClaimError as exc:
                if not config.shard.standby_when_full:
                    logger.error("Shard claim failed: %s", exc)
                    return
                if not standby_prepared:
                    logger.warning(
                        "Shard pool full; preparing hot standby while waiting (%s)",
                        exc,
                    )
                    print(
                        "[SHARD] Entering standby mode; logging in and loading extensions"
                    )
                    try:
                        await bot.prepare_standby(config.token)
                    except Exception:
                        logger.exception("Failed to prepare standby login")
                        await asyncio.sleep(config.shard.standby_poll_seconds)
                        continue
                    standby_prepared = True
                released = await mysql.recover_stuck_shards(
                    stale_after_seconds,
                    instance_stale_after_seconds=instance_failover_after_seconds,
                )
                if released:
                    logger.warning(
                        "Recovered %s stale shard(s) while waiting to claim",
                        released,
                    )
                    continue
                logger.warning(
                    "Shard pool full; standby for %ss before retrying (%s)",
                    config.shard.standby_poll_seconds,
                    exc,
                )
                print(
                    f"[SHARD] Waiting {config.shard.standby_poll_seconds}s before retrying shard claim: {exc}"
                )
                await asyncio.sleep(config.shard.standby_poll_seconds)

        print(
            f"[SHARD] Instance {config.shard.instance_id} claimed shard "
            f"{shard_assignment.shard_id}/{shard_assignment.shard_count}"
        )

        bot.set_shard_assignment(shard_assignment)
        await bot.push_status("starting")

        if standby_prepared:
            print("[SHARD] Standby takeover complete; connecting to gateway")
            await bot.connect(reconnect=True)
        else:
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
        try:
            await mysql.clear_instance_heartbeat(config.shard.instance_id)
        except Exception:
            logger.exception("Failed to clear instance heartbeat")
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

