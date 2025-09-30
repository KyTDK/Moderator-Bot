from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)

class LocaleRepository:
    """Loads translation files from disk and caches them in memory."""

    def __init__(
        self,
        locales_root: Path,
        *,
        default_locale: str = "en",
        fallback_locale: str | None = None,
    ) -> None:
        self._locales_root = Path(locales_root).resolve()
        self._default_locale = default_locale
        self._fallback_locale = fallback_locale or default_locale
        self._cache: dict[str, dict[str, Any]] = {}
        self._lock = Lock()
        self._loaded = False
        self._warn_locales = {
            value.lower()
            for value in (self._default_locale, self._fallback_locale)
            if value
        }

    @property
    def locales_root(self) -> Path:
        return self._locales_root

    @property
    def default_locale(self) -> str:
        return self._default_locale

    @property
    def fallback_locale(self) -> str:
        return self._fallback_locale

    def ensure_loaded(self) -> None:
        if not self._loaded:
            logger.info("LocaleRepository.ensure_loaded triggering reload")
            self.reload()
        else:
            logger.info("LocaleRepository.ensure_loaded found cache already loaded")

    def reload(self) -> None:
        logger.info("Reloading locale repository from disk (root=%s)", self._locales_root)
        new_cache = self._load_from_disk()
        with self._lock:
            self._cache = new_cache
            self._loaded = True
        logger.info("Locale cache reloaded (%d locales)", len(new_cache))
        
    async def reload_async(self) -> None:
        await asyncio.to_thread(self.reload)

    def list_locales(self) -> list[str]:
        self.ensure_loaded()
        with self._lock:
            return sorted(self._cache.keys())

    def get_locale_snapshot(self, locale: str) -> dict[str, Any]:
        self.ensure_loaded()
        with self._lock:
            return deepcopy(self._cache.get(locale, {}))

    def get_value(self, locale: str, key: str) -> Any:
        self.ensure_loaded()
        with self._lock:
            data = self._cache.get(locale)
        if data is None:
            self._log_missing_locale(locale)
            return None
        return self._resolve_key(locale, data, key)

    def _load_from_disk(self) -> dict[str, dict[str, Any]]:
        cache: dict[str, dict[str, Any]] = {}
        root = self._locales_root
        if not root.exists():
            logger.warning("Locales directory %s does not exist; cache cleared", root)
            return {}

        for path in sorted(root.rglob("*.json")):
            if any(part.startswith(".") for part in path.relative_to(root).parts):
                continue
            logger.info("Loading translation file %s", path)
            payload = self._read_json(path)
            if payload is None:
                continue
            locale = path.stem if path.parent == root else path.relative_to(root).parts[0]
            logger.info(
                "Merging payload for locale %s from file %s (keys=%d)",
                locale,
                path,
                len(payload),
            )
            cache[locale] = self._deep_merge(cache.get(locale, {}), payload)
        return cache

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            logger.warning("Translation file disappeared while reading: %s", path)
            return None
        except json.JSONDecodeError as exc:
            logger.error("Failed to parse translation file %s: %s", path, exc)
            return None
        if isinstance(data, Mapping):
            logger.info("Successfully parsed translation file %s", path)
            return dict(data)
        logger.warning("Ignoring translation file %s with non-object root", path)
        return None

    @classmethod
    def _deep_merge(cls, base: dict[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
        merged = dict(base)
        for key, value in incoming.items():
            if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
                merged[key] = cls._deep_merge(merged[key], value)
            else:
                merged[key] = deepcopy(value)
        return merged

    def _should_warn_for_locale(self, locale: str) -> bool:
        return locale.lower() in self._warn_locales

    def _log_missing_locale(self, locale: str) -> None:
        level = logging.WARNING if self._should_warn_for_locale(locale) else logging.DEBUG
        logger.log(level, "Requested locale %s missing from cache", locale)

    def _resolve_key(self, locale: str, data: dict[str, Any] | None, key: str) -> Any:
        if data is None:
            return None
        cursor: Any = data
        for part in key.split('.'):
            if isinstance(cursor, Mapping) and part in cursor:
                cursor = cursor[part]
            else:
                level = (
                    logging.WARNING
                    if self._should_warn_for_locale(locale)
                    else logging.DEBUG
                )
                logger.log(
                    level,
                    "Key %s missing while traversing part %s for locale %s (locale data available=%s)",
                    key,
                    part,
                    locale,
                    cursor is not None,
                )
                return None
        return cursor


__all__ = ["LocaleRepository"]
