from __future__ import annotations

import asyncio
import logging
import os
from typing import Callable

import discord

from modules.core.moderator_bot.guild_locale import GuildLocaleMixin
from modules.post_stats.topgg_poster import start_topgg_poster
from modules.utils import mysql


class BackgroundTaskMixin(GuildLocaleMixin):
    """Mixin providing lifecycle helpers relying on background tasks."""

    _mysql_initialisation_task: asyncio.Task[None] | None
    _extension_loader_task: asyncio.Task[None] | None
    _topgg_poster_task: asyncio.Task[None] | None
    _guild_sync_task: asyncio.Task[None] | None

    def _ensure_background_task(self, attr: str, factory: Callable[[], asyncio.Task[None]]) -> None:
        task: asyncio.Task[None] | None = getattr(self, attr)
        if task is None or task.done():
            task = factory()
            setattr(self, attr, task)

    async def _wait_for_client_ready(self) -> None:
        """Block until discord.Client has been logged in and is ready-aware."""
        while True:
            try:
                await self.wait_until_ready()
            except RuntimeError:
                await asyncio.sleep(0.5)
                continue
            else:
                return

    def _ensure_mysql_initialisation_started(self) -> None:
        def start() -> asyncio.Task[None]:
            return asyncio.create_task(self._initialise_mysql_pool())

        self._ensure_background_task("_mysql_initialisation_task", start)

    async def _initialise_mysql_pool(self) -> None:
        print("[STARTUP] Initialising MySQL pool in background")
        try:
            await mysql.initialise_and_get_pool()
        except Exception as exc:
            print(f"[FATAL] MySQL init failed: {exc}")
            raise

        print("[STARTUP] MySQL connection pool initialised")
        await mysql.update_instance_heartbeat(self._instance_id)
        print("[STARTUP] Instance heartbeat registered")

        for loop_task in (self.cleanup_task, self.shard_heartbeat, self.instance_heartbeat):
            if not loop_task.is_running():
                try:
                    loop_task.start()
                except RuntimeError:
                    pass
        print("[STARTUP] Core background loops ensured (cleanup/shard/instance)")

        self._ensure_background_task(
            "_guild_cache_preload_task",
            lambda: asyncio.create_task(self._preload_guild_locale_cache_when_ready()),
        )

    async def _wait_for_mysql_ready(self) -> None:
        task = self._mysql_initialisation_task
        if task is None:
            self._ensure_mysql_initialisation_started()
            task = self._mysql_initialisation_task

        if task is not None:
            await task

    async def _preload_guild_locale_cache_when_ready(self) -> None:
        await self._wait_for_client_ready()
        await self._wait_for_mysql_ready()
        await self._preload_guild_locale_cache()
        print("[STARTUP] Guild locale cache preloaded (post-ready)")

    async def _load_extensions_when_ready(self) -> None:
        await self._wait_for_client_ready()
        await self._wait_for_mysql_ready()
        await self._ensure_i18n_ready()
        await self._load_extensions()
        # Ensure the command tree is re-synchronised now that all extensions are loaded
        self._schedule_command_tree_sync(force=True)
        print("[STARTUP] Extension loader finished (post-ready)")

    async def _start_topgg_poster_when_ready(self) -> None:
        await self._wait_for_client_ready()
        await self._wait_for_mysql_ready()
        try:
            start_topgg_poster(self)
        except Exception as exc:
            print(f"[WARN] top.gg poster could not start: {exc}")
        else:
            print("[STARTUP] top.gg poster task started (post-ready)")

    async def _sync_single_guild_with_database(self, guild: discord.Guild) -> bool:
        try:
            preferred = getattr(guild, "preferred_locale", None)
            from modules.i18n.locale_utils import normalise_locale  # local import to avoid cycle

            normalized_locale = normalise_locale(preferred)
            owner_id = await self._resolve_guild_owner_id(guild)
            if owner_id is None:
                logging.getLogger(__name__).warning(
                    "Skipping guild sync for %s (%s); owner ID unavailable",
                    guild.id,
                    guild.name,
                )
                return False
            await mysql.add_guild(guild.id, guild.name, owner_id, normalized_locale)
            await self.refresh_guild_locale_override(guild.id)
        except Exception as exc:
            print(f"[ERROR] Failed to sync guild {guild.id}: {exc}")
            return False

        return True

    async def _sync_guilds_with_database(self) -> None:
        await self._wait_for_mysql_ready()

        guilds = list(self.guilds)
        if not guilds:
            print("[STARTUP] No guilds to synchronise")
            return

        semaphore = asyncio.Semaphore(10)
        synced = 0

        async def worker(guild: discord.Guild) -> None:
            nonlocal synced
            async with semaphore:
                if await self._sync_single_guild_with_database(guild):
                    synced += 1

        await asyncio.gather(*(worker(guild) for guild in guilds))
        print(
            f"[STARTUP] Synced {synced}/{len(guilds)} guilds with the database (background)"
        )

    async def _load_extensions(self) -> None:
        try:
            for filename in os.listdir("./cogs"):
                path = os.path.join("cogs", filename)
                if not (os.path.isfile(path) and filename.endswith(".py")):
                    continue
                try:
                    await self.load_extension(f"cogs.{filename[:-3]}")
                    if self._log_cog_loads:
                        print(f"Loaded Cog: {filename[:-3]}")
                except Exception as exc:
                    print(f"[FATAL] Failed to load cog {filename}: {exc}")
                    raise
        except Exception:
            raise

    async def push_status(self, status: str, *, last_error: str | None = None) -> None:
        if self._shard_assignment is None:
            return

        await self._wait_for_mysql_ready()

        ws = getattr(self, "ws", None)
        session_id = getattr(ws, "session_id", None)
        resume_url = getattr(ws, "resume_url", None)
        try:
            await mysql.update_shard_status(
                shard_id=self._shard_assignment.shard_id,
                instance_id=self._instance_id,
                status=status,
                session_id=session_id,
                resume_url=resume_url,
                last_error=last_error,
            )
        except Exception:
            logging.getLogger(__name__).exception("Failed to update shard status (%s)", status)

    def set_shard_assignment(self, shard_assignment: mysql.ShardAssignment) -> None:
        """Attach a shard assignment to the bot (used for standby takeover)."""

        self._shard_assignment = shard_assignment
        self.shard_id = shard_assignment.shard_id
        self.shard_count = shard_assignment.shard_count
        self._connection.shard_id = shard_assignment.shard_id
        self._connection.shard_count = shard_assignment.shard_count

    async def prepare_standby(self, token: str) -> None:
        """Log in without connecting so the bot is ready for a fast takeover."""

        if self._standby_login_performed:
            return

        await self.login(token)
        self._standby_login_performed = True
