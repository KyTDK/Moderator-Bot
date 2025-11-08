from __future__ import annotations

import asyncio
import logging

from discord.ext import commands

from modules.core.moderator_bot import ModeratorBot
from modules.nsfw_scanner.custom_blocks.config import CustomBlockStreamConfig
from modules.nsfw_scanner.custom_blocks.stream import CustomBlockStreamProcessor

log = logging.getLogger(__name__)


class CustomBlockCog(commands.Cog):
    """Start the dashboard custom image upload stream processor."""

    def __init__(self, bot: ModeratorBot) -> None:
        self.bot = bot
        self._config = CustomBlockStreamConfig.from_env()
        self._stream_processor = CustomBlockStreamProcessor(bot, self._config)
        self._stream_task: asyncio.Task[None] | None = None

    async def cog_load(self) -> None:
        if not self._config.enabled:
            log.debug("Custom block stream disabled; skipping startup.")
            return

        if self._stream_task is None or self._stream_task.done():
            task = asyncio.create_task(
                self._ensure_stream_started(),
                name="custom-block-stream-start",
            )
            self._stream_task = task
            task.add_done_callback(
                lambda t, owner=self: setattr(owner, "_stream_task", None)
                if owner._stream_task is t
                else None
            )

    async def cog_unload(self) -> None:
        if self._stream_task is not None:
            self._stream_task.cancel()
            try:
                await self._stream_task
            except asyncio.CancelledError:
                pass
            finally:
                self._stream_task = None

        await self._stream_processor.stop()

    async def _ensure_stream_started(self) -> None:
        await self.bot.wait_until_ready()
        wait_mysql = getattr(self.bot, "_wait_for_mysql_ready", None)
        if callable(wait_mysql):
            try:
                await wait_mysql()
            except Exception:
                log.exception(
                    "Failed waiting for MySQL readiness before starting custom block stream",
                )
                return

        try:
            await self._stream_processor.start()
        except Exception:
            log.exception("Failed to start custom block Redis stream processor")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CustomBlockCog(bot))  # type: ignore[arg-type]
