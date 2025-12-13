"""Shared helpers for working with the Fernet secret key.

This module provides a single place where we resolve the Fernet secret key so
that every caller gets identical behavior and we can safely fall back to a
development key when the environment variable is absent. The fallback makes it
possible to run the bot locally without configuring secrets while still loudly
warning that the key must be provided in production.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache

from cryptography.fernet import Fernet
from dotenv import load_dotenv

load_dotenv()

_ENV_NAME = "FERNET_SECRET_KEY"

# A deterministic (but insecure) fallback key so local development keeps working
# even when FERNET_SECRET_KEY is not configured. Never rely on this key in
# production – it is public.
_DEFAULT_FERNET_KEY = "bW9kZXJhdG9yLWJvdC1mZXJuZXQta2V5LWRldi0zMiE="

_LOGGER = logging.getLogger(__name__)
_FERNET_KEY_PROVIDED = bool(os.getenv(_ENV_NAME))


def _resolve_fernet_key() -> str:
    key = os.getenv(_ENV_NAME)
    if key:
        return key

    os.environ.setdefault(_ENV_NAME, _DEFAULT_FERNET_KEY)
    _LOGGER.warning(
        "%s is not set; using a built-in development fallback key. "
        "This key is public and must not be used in production.",
        _ENV_NAME,
    )
    return _DEFAULT_FERNET_KEY


def _validated_fernet_key() -> str:
    """Return a Fernet-compatible key, falling back to the dev key when invalid."""

    key = _resolve_fernet_key()
    try:
        Fernet(key)
        return key
    except (ValueError, TypeError):
        _LOGGER.warning(
            "%s is invalid; using a built-in development fallback key. "
            "This key is public and must not be used in production.",
            _ENV_NAME,
            exc_info=True,
        )
        os.environ[_ENV_NAME] = _DEFAULT_FERNET_KEY
        return _DEFAULT_FERNET_KEY


def get_fernet_key() -> str:
    """Return the configured Fernet key or the development fallback."""

    return _validated_fernet_key()


@lru_cache(maxsize=1)
def get_fernet() -> Fernet:
    """Return a cached Fernet instance so every consumer shares the same key."""

    return Fernet(_validated_fernet_key())


def is_fernet_configured() -> bool:
    """Return True when the runtime was provided a Fernet key via the env."""

    return _FERNET_KEY_PROVIDED
