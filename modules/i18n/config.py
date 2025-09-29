from __future__ import annotations

"""Helpers for building i18n components used by the moderator bot."""

import logging
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


def _unique(paths: Iterable[Path]) -> list[Path]:
    seen: set[Path] = set()
    ordered: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        ordered.append(resolved)
        seen.add(resolved)
    return ordered


def resolve_locales_root(configured_root: str | None, repo_root: Path) -> tuple[Path, bool]:
    """Return the locales directory to use and whether the configured path is missing."""

    raw = Path(configured_root).expanduser() if configured_root else Path("locales")

    candidates: list[Path] = []
    if raw.is_absolute():
        candidates.append(raw)
    else:
        candidates.extend((Path.cwd() / raw, repo_root / raw))

    bundled_default = repo_root / "locales"
    candidates.append(bundled_default)

    unique_candidates = _unique(candidates)

    logger.info(
        "Resolving locales root (configured=%s, repo_root=%s, candidates=%s)",
        configured_root,
        repo_root,
        unique_candidates,
    )

    for candidate in unique_candidates:
        if candidate.exists():
            logger.info("Locales root resolved to %s (exists=%s)", candidate, candidate.exists())
            return candidate, bool(configured_root) and candidate != raw.resolve()

    fallback = unique_candidates[0]
    logger.warning(
        "Falling back to locales root %s (configured missing=%s)",
        fallback,
        bool(configured_root),
    )
    return fallback, bool(configured_root)


__all__ = ["resolve_locales_root"]

