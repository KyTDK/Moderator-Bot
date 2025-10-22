from __future__ import annotations

import asyncio
import logging

from modules.core.moderator_bot.i18n import I18nMixin

_logger = logging.getLogger(__name__)


class CommandTreeSyncMixin(I18nMixin):
    """Mixin encapsulating Discord command tree synchronisation."""

    _command_tree_sync_task: asyncio.Task[None] | None
    _command_tree_sync_retry_seconds: float

    async def ensure_command_tree_translator(self) -> None:
        """Ensure the Discord command tree translator is set before syncing."""

        await self._ensure_i18n_ready()

        translator = self._command_tree_translator
        if translator is None:
            _logger.warning("Command tree translator requested but not initialised")
            return

        if not self._command_tree_translator_loaded:
            _logger.debug("Loading command tree translator before activation")
            await translator.load()
            self._command_tree_translator_loaded = True

        current = getattr(self.tree, "translator", None)
        if current is translator:
            return

        _logger.debug("Setting Discord command tree translator prior to sync")
        await self.tree.set_translator(translator)

    def _schedule_command_tree_sync(self, *, delay: float = 0.0, force: bool = False) -> None:
        if self.is_closed():
            print("[STARTUP] Command tree sync scheduling skipped; bot is closed")
            return

        current = self._command_tree_sync_task
        if current is not None:
            if current.done():
                print("[STARTUP] Previous command tree sync task finished; scheduling fresh run")
                self._command_tree_sync_task = None
            elif not force:
                print("[STARTUP] Command tree sync already pending; ignoring duplicate schedule request")
                return
            else:
                current.cancel()
                print("[STARTUP] Cancelling in-flight command tree sync for forced reschedule")

        print(f"[STARTUP] Scheduling command tree sync (delay={delay:.1f}s, force={force})")
        self._command_tree_sync_task = asyncio.create_task(
            self._run_command_tree_sync(delay=delay)
        )

    async def _run_command_tree_sync(self, *, delay: float = 0.0) -> None:
        should_retry = False
        try:
            if delay > 0:
                print(f"[STARTUP] Command tree sync sleeping for {delay:.1f}s before running")
                await asyncio.sleep(delay)

            await self.wait_until_ready()
            print("[STARTUP] Command tree sync coroutine has bot ready; beginning attempts")
            max_attempts = 3
            retry_delay = 10.0
            timeout = 45.0

            for attempt in range(1, max_attempts + 1):
                print(f"[STARTUP] Command tree sync attempt {attempt}/{max_attempts}")
                if self.is_closed():
                    print("[STARTUP] Command tree sync aborting; bot is closed before completion")
                    return
                try:
                    await self.ensure_command_tree_translator()
                    print("[STARTUP] Command tree translator ensured before sync")
                    await asyncio.wait_for(self.tree.sync(guild=None), timeout=timeout)
                except asyncio.TimeoutError:
                    print(
                        f"[WARN] Command tree sync timed out after {timeout}s "
                        f"(attempt {attempt}/{max_attempts}); retrying in {retry_delay}s"
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    print(
                        f"[ERROR] Command tree sync failed on attempt {attempt}/{max_attempts}: {exc}"
                    )
                else:
                    print(
                        f"[COMMANDS] Command tree sync completed on attempt {attempt}"
                    )
                    return

                if attempt < max_attempts:
                    await asyncio.sleep(retry_delay)

            print(
                f"[WARN] Command tree sync abandoned after {max_attempts} attempts; commands may be outdated."
            )
            should_retry = True
        finally:
            self._command_tree_sync_task = None
            if should_retry and not self.is_closed():
                print(
                    f"[STARTUP] Command tree sync retry scheduled in {self._command_tree_sync_retry_seconds:.1f}s"
                )
                self._schedule_command_tree_sync(
                    delay=self._command_tree_sync_retry_seconds,
                    force=False,
                )
