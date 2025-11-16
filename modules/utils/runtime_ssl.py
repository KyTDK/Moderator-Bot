from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Optional

_LOGGER = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def ensure_certifi_trust_store() -> Optional[str]:
    """Ensure Python knows where to find a CA bundle for HTTPS clients.

    Some macOS Python installs ship without a populated trust store. When that
    happens aiohttp (and therefore discord.py) will raise an SSL handshake
    failure before the bot can even connect. If certifi is installed we point
    the relevant environment variables at its bundle so OpenSSL can validate
    certificates normally.
    """

    cafile = os.environ.get("SSL_CERT_FILE")
    if cafile:
        return cafile

    try:
        import certifi  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on optional dependency
        _LOGGER.debug("certifi not available; unable to auto-configure SSL: %s", exc)
        return None

    cafile = certifi.where()
    os.environ.setdefault("SSL_CERT_FILE", cafile)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", cafile)
    os.environ.setdefault("AWS_CA_BUNDLE", cafile)
    _LOGGER.info("Configured SSL trust store via certifi bundle at %s", cafile)
    return cafile
