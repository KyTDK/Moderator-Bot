from __future__ import annotations

import asyncio
import logging
import os
from contextlib import AbstractContextManager
from contextvars import Token
from pathlib import Path
from typing import Any

from modules.i18n import LocaleRepository, Translator
from modules.i18n.config import resolve_locales_root
from modules.i18n.discord_translator import DiscordAppCommandTranslator
from modules.i18n.guild_cache import GuildLocaleCache
from modules.i18n.resolution import LocaleResolution, LocaleResolver
from modules.i18n.service import TranslationService

_logger = logging.getLogger(__name__)


class I18nMixin:
    """Mixin providing translation-related helpers for the moderator bot."""

    _locale_repository: LocaleRepository | None
    _translator: Translator | None
    _translation_service: TranslationService | None
    _command_tree_translator: DiscordAppCommandTranslator | None
    _command_tree_translator_loaded: bool
    _guild_locales: GuildLocaleCache
    _locale_resolver: LocaleResolver
    _i18n_bootstrap_task: asyncio.Task[None] | None

    def _initialise_i18n(self) -> None:
        if self._translator is not None:
            return

        default_locale = os.getenv("I18N_DEFAULT_LOCALE", "en")
        fallback_locale = os.getenv("I18N_FALLBACK_LOCALE") or default_locale

        configured_root = os.getenv("I18N_LOCALES_DIR")
        legacy_root = os.getenv("LOCALES_DIR")
        if not configured_root and legacy_root:
            _logger.warning(
                "Environment variable LOCALES_DIR is deprecated; please migrate to "
                "I18N_LOCALES_DIR. Using legacy value for now.",
            )
            configured_root = legacy_root

        _logger.warning(
            "Initialising i18n (default=%r, fallback=%r, locales_dir=%r)",
            os.getenv("I18N_DEFAULT_LOCALE"),
            os.getenv("I18N_FALLBACK_LOCALE"),
            configured_root,
        )

        repo_root = Path(__file__).resolve().parents[3]
        locales_root, missing_configured = resolve_locales_root(configured_root, repo_root)

        if configured_root and missing_configured:
            _logger.warning(
                "Configured locales directory %s not found; using %s instead",
                Path(configured_root).expanduser().resolve(),
                locales_root,
            )

        if not locales_root.exists():
            _logger.warning(
                "Locales directory %s does not exist; translations will fall back to keys",
                locales_root,
            )

        _logger.warning(
            "Initialising locale repository (root=%s, default=%s, fallback=%s)",
            locales_root,
            default_locale,
            fallback_locale,
        )

        self._locale_repository = LocaleRepository(
            locales_root,
            default_locale=default_locale,
            fallback_locale=fallback_locale,
        )

        _logger.warning("Ensuring locale repository is loaded")
        self._locale_repository.ensure_loaded()
        try:
            available_locales = self._locale_repository.list_locales()
        except Exception:  # pragma: no cover - defensive logging
            _logger.exception("Failed to list locales after loading repository")
        else:
            _logger.warning(
                "Locale repository ready with %d locales: %s",
                len(available_locales),
                available_locales,
            )

        self._translator = Translator(self._locale_repository)
        _logger.warning(
            "Translator initialised (default=%s, fallback=%s)",
            self._translator.default_locale,
            self._translator.fallback_locale,
        )
        self._translation_service = TranslationService(self._translator)
        _logger.warning("Translation service ready; preparing Discord translator")
        self._command_tree_translator = DiscordAppCommandTranslator(self._translation_service)
        self._command_tree_translator_loaded = False

    async def _initialise_i18n_async(self) -> None:
        await asyncio.to_thread(self._initialise_i18n)

    async def _ensure_i18n_ready(self) -> None:
        if self._translator is not None:
            return

        task = self._i18n_bootstrap_task
        if task is None or task.done():
            task = asyncio.create_task(self._initialise_i18n_async())
            self._i18n_bootstrap_task = task

        await task

    async def ensure_i18n_ready(self) -> None:
        await self._ensure_i18n_ready()

    @property
    def translator(self) -> Translator:
        if self._translator is None:
            raise RuntimeError("Translator has not been initialised")
        return self._translator

    @property
    def locale_repository(self) -> LocaleRepository:
        if self._locale_repository is None:
            raise RuntimeError("Locale repository has not been initialised")
        return self._locale_repository

    async def refresh_translations(self, *, fetch: bool = True) -> None:
        if self._locale_repository is None:
            _logger.warning("Translation refresh requested but no locale repository is configured")
            return

        if fetch:
            _logger.warning("Crowdin integration has been removed; reloading translations from disk")
        await self._locale_repository.reload_async()
        _logger.warning("Translations reloaded from disk")

    def translate(
        self,
        key: str,
        *,
        guild_id: int | None = None,
        locale: str | None = None,
        placeholders: dict[str, Any] | None = None,
        fallback: str | None = None,
    ) -> Any:
        service = self._translation_service
        if service is None:
            _logger.warning("Translation requested but translator has not been initialised")
            return fallback if fallback is not None else key

        resolved_locale = locale
        if resolved_locale is None and guild_id is not None:
            try:
                resolved_locale = self.get_guild_locale(guild_id)
            except ValueError:
                _logger.warning(
                    "Unable to determine guild locale from guild_id=%s; using defaults",
                    guild_id,
                )

        return service.translate(
            key,
            locale=resolved_locale,
            placeholders=placeholders,
            fallback=fallback,
        )

    @property
    def translation_service(self) -> TranslationService:
        if self._translation_service is None:
            raise RuntimeError("Translation service has not been initialised")
        return self._translation_service

    def push_locale(self, locale: Any | None) -> Token[str | None]:
        return self.translation_service.push_locale(locale)

    def reset_locale(self, token: Token[str | None]) -> None:
        self.translation_service.reset_locale(token)

    def use_locale(self, locale: Any | None) -> AbstractContextManager[None]:
        return self.translation_service.use_locale(locale)

    def current_locale(self) -> str | None:
        return self.translation_service.current_locale()

    def infer_locale(self, *candidates: Any) -> LocaleResolution:
        """Infer the locale for the provided *candidates*."""

        return self._locale_resolver.infer(*candidates)

    def resolve_locale(self, *candidates: Any) -> str | None:
        """Convenience wrapper returning the resolved locale string."""

        return self.infer_locale(*candidates).resolved()
