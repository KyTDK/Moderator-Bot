from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from threading import RLock
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

class LocaleRepository:
    """Loads translation files from disk and caches them in memory."""

    def __init__(
        self,
        locales_root: Path,
        *,
        default_locale: str = "en",
        fallback_locale: Optional[str] = None,
    ) -> None:
        self._locales_root = Path(locales_root).resolve()
        self._default_locale = default_locale
        self._fallback_locale = fallback_locale or default_locale
        self._cache: dict[str, dict[str, Any]] = {}
        self._lock = RLock()
        self._loaded = False

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
            self.reload()

    def reload(self) -> None:
        new_cache = self._load_from_disk()
        with self._lock:
            self._cache = new_cache
            self._loaded = True
        logger.debug("Locale cache reloaded (%d locales)", len(new_cache))

    async def reload_async(self) -> None:
        await asyncio.to_thread(self.reload)

    def refresh(self) -> None:
        self.reload()

    async def refresh_async(self) -> None:
        await asyncio.to_thread(self.refresh)

    def list_locales(self) -> list[str]:
        self.ensure_loaded()
        with self._lock:
            return sorted(self._cache.keys())

    def get_locale_snapshot(self, locale: str) -> dict[str, Any]:
        self.ensure_loaded()
        with self._lock:
            data = self._cache.get(locale, {})
            return deepcopy(data)

    def get_value(self, locale: str, key: str) -> Any:
        self.ensure_loaded()
        with self._lock:
            data = self._cache.get(locale)
        return self._resolve_key(data, key)

    def get_raw_locale(self, locale: str) -> dict[str, Any] | None:
        self.ensure_loaded()
        with self._lock:
            return self._cache.get(locale)

    def _load_from_disk(self) -> dict[str, dict[str, Any]]:
        cache: Dict[str, dict[str, Any]] = {}
        root = self._locales_root
        if not root.exists():
            logger.warning("Locales directory %s does not exist; cache cleared", root)
            return {}

        for entry in root.iterdir():
            if entry.name.startswith("."):
                continue
            if entry.is_file() and entry.suffix.lower() == ".json":
                locale_code = entry.stem
                payload = self._read_json(entry)
                if payload is not None:
                    cache[locale_code] = payload
                    logger.debug(
                        "Loaded translation file %s for locale %s with %d top-level keys",
                        entry,
                        locale_code,
                        len(payload),
                    )
            elif entry.is_dir():
                locale_code = entry.name
                locale_data: dict[str, Any] = {}
                loaded_files: list[Path] = []
                for json_path in sorted(entry.rglob("*.json")):
                    payload = self._read_json(json_path)
                    if payload is not None:
                        locale_data = self._deep_merge(locale_data, payload)
                        loaded_files.append(json_path)
                        logger.debug(
                            "Merged translation file %s into locale %s with %d top-level keys",
                            json_path,
                            locale_code,
                            len(payload),
                        )
                cache[locale_code] = locale_data
                if loaded_files:
                    logger.debug(
                        "Locale %s aggregated from %d translation files: %s",
                        locale_code,
                        len(loaded_files),
                        ", ".join(str(path) for path in loaded_files),
                    )

        return cache

    @staticmethod
    def _read_json(path: Path) -> Optional[dict[str, Any]]:
        try:
            # Use ``utf-8-sig`` so we transparently handle files that were saved
            # with a UTF-8 BOM. Some of the translation assets bundled with the
            # bot include this marker which previously caused ``json.load`` to
            # raise ``JSONDecodeError`` and prevented the locale cache from
            # being populated. Falling back to the translation key string then
            # triggered ``TypeError`` at runtime when code expected a mapping.
            with path.open("r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            logger.warning("Translation file disappeared while reading: %s", path)
            return None
        except json.JSONDecodeError as exc:
            logger.error("Failed to parse translation file %s: %s", path, exc)
            return None
        if isinstance(data, Mapping):
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

    @staticmethod
    def _resolve_key(data: Optional[dict[str, Any]], key: str) -> Any:
        if data is None:
            return None
        cursor: Any = data
        for part in key.split('.'):
            if isinstance(cursor, Mapping) and part in cursor:
                cursor = cursor[part]
            else:
                return None
        return cursor


__all__ = ["LocaleRepository"]
