from __future__ import annotations

import asyncio
import logging
import time

from modules.core import configure_logging, load_runtime_config
from modules.core.moderator_bot import ModeratorBot
from modules.utils import mysql

print(f"[BOOT] Starting Moderator Bot at {time.strftime('%X')}")

async def _main() -> None:
    print("[TRACE] Loading runtime config...")
    config = load_runtime_config()
    print("[TRACE] Runtime config loaded")
    configure_logging(config.log_level)
    print(f"[TRACE] Logging configured at level {config.log_level}")
    logger = logging.getLogger("moderator.startup")
    logger.info("Log level resolved to %s", config.log_level)
    logger.info("Shard settings: instance=%s total=%s preferred=%s heartbeat=%ss instance_heartbeat=%ss standby=%s poll_interval=%ss",
                config.shard.instance_id,
                config.shard.total_shards,
                config.shard.preferred_shard,
                config.shard.heartbeat_seconds,
                config.shard.instance_heartbeat_seconds,
                config.shard.standby_when_full,
                config.shard.standby_poll_seconds)
    logger.info("Cog load logging enabled: %s", config.log_cog_loads)

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
        logger.info("Initialising MySQL connection pool")
        print("[TRACE] Initialising MySQL pool...")
        await mysql.initialise_and_get_pool()
        logger.info("MySQL pool initialised successfully")
        print("[TRACE] MySQL pool initialised")
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
        logger.info("Recovery windows: failover=%ss stale=%ss instance_failover=%ss",
                    failover_after_seconds,
                    stale_after_seconds,
                    instance_failover_after_seconds)
        print(
            f"[TRACE] Shard timing windows -> failover={failover_after_seconds}s, stale={stale_after_seconds}s, instance={instance_failover_after_seconds}s"
        )
        logger.info("Recovering stale shard assignments before claiming")
        print("[TRACE] Running shard recovery pass...")
        await mysql.recover_stuck_shards(
            stale_after_seconds,
            instance_stale_after_seconds=instance_failover_after_seconds,
        )
        while True:
            try:
                logger.info("Updating heartbeat for instance %s", config.shard.instance_id)
                print(f"[TRACE] Updating heartbeat for {config.shard.instance_id}")
                await mysql.update_instance_heartbeat(config.shard.instance_id)
                logger.info("Heartbeat update persisted for %s", config.shard.instance_id)
                print(f"[TRACE] Heartbeat updated for {config.shard.instance_id}")
                logger.info("Attempting shard claim (preferred=%s, total=%s)",
                            config.shard.preferred_shard,
                            config.shard.total_shards)
                print(f"[TRACE] Attempting shard claim (preferred={config.shard.preferred_shard})")
                shard_assignment = await mysql.claim_shard(
                    config.shard.instance_id,
                    total_shards=config.shard.total_shards,
                    preferred_shard=config.shard.preferred_shard,
                    stale_after_seconds=stale_after_seconds,
                    instance_stale_after_seconds=instance_failover_after_seconds,
                )
                logger.info("Shard claim succeeded: shard=%s/%s",
                            shard_assignment.shard_id,
                            shard_assignment.shard_count)
                print(f"[TRACE] Shard claim succeeded: {shard_assignment.shard_id}/{shard_assignment.shard_count}")
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

        logger.info("Applying shard assignment to bot")
        print("[TRACE] Applying shard assignment to bot")
        bot.set_shard_assignment(shard_assignment)
        await bot.push_status("starting")
        logger.info("Shard status pushed: starting")
        print("[TRACE] Marked bot status as starting")

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
        logger.info("Entering shutdown cleanup")
        print("[TRACE] Entering shutdown cleanup sequence")
        if bot is not None and not bot.is_closed():
            logger.info("Closing bot connection")
            print("[TRACE] Closing bot connection")
            try:
                await bot.close()
                logger.info("Bot connection closed cleanly")
                print("[TRACE] Bot closed cleanly")
            except Exception:
                logger.exception("Failed to close bot cleanly")
        try:
            logger.info("Clearing instance heartbeat for %s", config.shard.instance_id)
            await mysql.clear_instance_heartbeat(config.shard.instance_id)
            logger.info("Instance heartbeat cleared for %s", config.shard.instance_id)
            print(f"[TRACE] Cleared instance heartbeat for {config.shard.instance_id}")
        except Exception:
            logger.exception("Failed to clear instance heartbeat")
        if shard_assignment is not None:
            try:
                logger.info("Releasing shard %s for instance %s", shard_assignment.shard_id, config.shard.instance_id)
                released = await mysql.release_shard(shard_assignment.shard_id, config.shard.instance_id)
                if released:
                    logger.info("Shard %s released", shard_assignment.shard_id)
                    print(f"[TRACE] Released shard {shard_assignment.shard_id}")
                else:
                    logger.warning("Release call reported no changes for shard %s", shard_assignment.shard_id)
            except Exception:
                logger.exception("Failed to release shard %s", shard_assignment.shard_id)
        try:
            logger.info("Closing MySQL pool")
            await mysql.close_pool()
            logger.info("MySQL pool closed")
            print("[TRACE] MySQL pool closed")
        except Exception:
            logger.exception("Failed to close MySQL pool cleanly")

if __name__ == "__main__":
    asyncio.run(_main())

