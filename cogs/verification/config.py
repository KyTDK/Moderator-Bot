from __future__ import annotations

import os
from urllib.parse import urlsplit, urlunsplit

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
    raw = os.getenv("CAPTCHA_PUBLIC_VERIFY_URL")
    if not raw:
        return DEFAULT_PUBLIC_VERIFY_URL

    cleaned = raw.strip()
    return cleaned or DEFAULT_PUBLIC_VERIFY_URL
