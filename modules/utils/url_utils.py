from __future__ import annotations

import re
from typing import Iterable, List
from urllib.parse import urlparse

from urlextract import URLExtract

_EXTRACTOR = URLExtract()

INTERMEDIARY_DOMAINS = {
    "antiphishing.biz",
}


def ensure_scheme(u: str) -> str:
    """Prepend http:// if no scheme is present."""

    if not u.startswith(("http://", "https://")):
        return f"http://{u}"
    return u


def _ensure_scheme(u: str) -> str:
    """Backward compatible alias for internal imports."""

    return ensure_scheme(u)

def _strip_leading_garbage(u: str) -> str:
    """
    If a string has multiple http(s):// substrings (e.g., copied embeds),
    keep the last one.
    """
    matches = list(re.finditer(r"https?://", u))
    if matches:
        return u[matches[-1].start():]
    return u

def clean_and_normalize_urls(found_urls: Iterable[str]) -> List[str]:
    """
    1) Ensure scheme (http://) if missing
    2) If multiple http(s):// occur, keep the last
    Returns a list of cleaned URL strings.
    """
    cleaned: List[str] = []
    for raw in found_urls:
        u = ensure_scheme(raw)
        u = _strip_leading_garbage(u)
        cleaned.append(u)
    return cleaned

def extract_urls(text: str, *, normalize: bool = True) -> List[str]:
    """
    Extract URLs from free text. If normalize=True, applies cleaning.
    Synchronous; fast path for most uses.
    """
    urls = _EXTRACTOR.find_urls(text or "")
    return clean_and_normalize_urls(urls) if normalize else urls

async def unshorten_url(url: str, *, timeout: float = 10.0) -> str:
    """
    Follow redirects and return the final resolved URL.
    Safe default UA; TLS verification off is sometimes necessary on Discord-shortened links
    """
    import httpx
    headers = {"User-Agent": "Mozilla/5.0 ModeratorBot"}
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            headers=headers,
            timeout=timeout,
            verify=False,
        ) as client:
            resp = await client.get(url)
            final_url = str(resp.url)
            if any(dom in final_url for dom in INTERMEDIARY_DOMAINS):
                return url
            return final_url
    except Exception:
        return url

async def extract_urls_expanded(
    text: str,
    *,
    normalize: bool = True,
    expand: bool = True,
    expand_timeout: float = 10.0,
) -> List[str]:
    """
    Extract URLs and (optionally) expand shorteners.
    - normalize: run clean_and_normalize_urls
    - expand: follow redirects per-URL (async)
    Returns a list of unique URLs preserving input order (stable de-dupe).
    """
    from collections import OrderedDict

    base = extract_urls(text, normalize=normalize)

    if not expand:
        # stable unique
        return list(OrderedDict.fromkeys(base))

    expanded: List[str] = []
    for u in base:
        eu = await unshorten_url(u, timeout=expand_timeout)
        expanded.append(eu)

    # stable de-dupe across original + expanded
    return list(OrderedDict.fromkeys(expanded))

def norm_domain(u: str) -> str:
    try:
        p = urlparse(u if u.startswith(("http://","https://")) else f"http://{u}")
        host = (p.netloc or "").lower()
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return u.lower()

def norm_url(u: str) -> str:
    s = u.strip().lower()
    if s.endswith("/"):
        s = s[:-1]
    return s

def update_tld_list() -> None:
    _EXTRACTOR.update()
