from __future__ import annotations

import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

DEFAULT_API_BASE = "https://modbot.neomechanical.com/api/captcha"
DEFAULT_PUBLIC_VERIFY_URL = "https://modbot.neomechanical.com/captcha"

def resolve_api_base() -> str:
    raw = os.getenv("CAPTCHA_PUBLIC_VERIFY_URL")
    if not raw:
        return DEFAULT_API_BASE

    base = raw.strip()
    if not base:
        return DEFAULT_API_BASE

    parts = urlsplit(base)
    path = parts.path

    if path.endswith("/start"):
        path = path[: -len("/start")]

    path = path.rstrip("/")

    if path.endswith("/accelerated/captcha"):
        path = f"{path[: -len('/accelerated/captcha')]}/api/captcha"
    elif path.endswith("/captcha"):
        path = f"{path[: -len('/captcha')]}/api/captcha"
    elif not path.endswith("/api/captcha"):
        if path:
            path = f"{path}/api/captcha"
        else:
            path = "/api/captcha"

    rebuilt = parts._replace(path=path, query="", fragment="")
    return urlunsplit(rebuilt).rstrip("/") or DEFAULT_API_BASE

def resolve_public_verify_url() -> str:
    """Return the public verification URL exposed to end users."""

    raw = os.getenv("CAPTCHA_PUBLIC_VERIFY_URL")
    if not raw:
        return DEFAULT_PUBLIC_VERIFY_URL

    cleaned = raw.strip()
    return cleaned or DEFAULT_PUBLIC_VERIFY_URL

def build_public_verification_url(public_base: str, guild_id: int) -> str:
    """Build a URL that points at the public verification UI for a guild."""

    base = public_base or DEFAULT_PUBLIC_VERIFY_URL
    parts = urlsplit(base)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["guildId"] = str(guild_id)
    new_query = urlencode(query)
    rebuilt = parts._replace(query=new_query)
    return urlunsplit(rebuilt)
