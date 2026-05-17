"""RSS/Atom auto-discovery helpers."""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

import httpx

_LINK_RE = re.compile(r"<link\b([^>]*?)(?:/>|>)", re.IGNORECASE | re.DOTALL)
_ATTR_RE = re.compile(
    r'([a-zA-Z][a-zA-Z0-9_-]*)\s*=\s*'
    r'(?:"([^"]*)"'
    r"|'([^']*)'"
    r"|([^\s>\"'/]+))",
    re.IGNORECASE,
)

_FEED_MIME_TYPES = frozenset({
    "application/rss+xml",
    "application/atom+xml",
    "application/feed+json",
    "text/xml",
    "application/xml",
})

# Probed in order when no <link> tags are found.
_COMMON_FEED_PATHS = [
    "/feed",
    "/feed/",
    "/rss",
    "/rss.xml",
    "/atom",
    "/atom.xml",
    "/feed.xml",
    "/index.xml",
    "/feeds/posts/default",  # Blogger
]

_HEADERS = {"User-Agent": "Lectio/1.0 (RSS auto-discovery; +https://github.com/joshg253/Lectio)"}


def _ct_is_feed(content_type: str) -> bool:
    base = content_type.split(";")[0].strip().lower()
    return base in _FEED_MIME_TYPES or "rss" in base or "atom" in base


def _parse_attrs(tag_body: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for m in _ATTR_RE.finditer(tag_body):
        key = m.group(1).lower()
        val = next((g for g in (m.group(2), m.group(3), m.group(4)) if g is not None), "")
        attrs.setdefault(key, val)
    return attrs


def discover_feed_urls(url: str, *, timeout: float = 10.0) -> list[str]:
    """Return RSS/Atom feed URLs reachable from url.

    If url itself is a feed, returns [url].
    If url is an HTML page, parses <link rel="alternate"> tags and probes
    common path suffixes.
    Returns [] on network failure or when nothing is found.
    """
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True, headers=_HEADERS)
    except Exception:
        return []

    if not resp.is_success:
        return []

    final_url = str(resp.url)
    ct = resp.headers.get("content-type", "")

    if _ct_is_feed(ct):
        return [final_url]

    # Parse HTML <link rel="alternate"> tags.
    candidates: list[str] = []
    for m in _LINK_RE.finditer(resp.text):
        attrs = _parse_attrs(m.group(1))
        rel = attrs.get("rel", "").lower()
        mtype = attrs.get("type", "").split(";")[0].strip().lower()
        href = attrs.get("href", "").strip()
        if "alternate" in rel and mtype in _FEED_MIME_TYPES and href:
            absolute = urljoin(final_url, href)
            if absolute not in candidates:
                candidates.append(absolute)

    if candidates:
        return candidates

    # Probe common path suffixes with HEAD requests.
    parsed = urlparse(final_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    for suffix in _COMMON_FEED_PATHS:
        probe_url = origin + suffix
        try:
            head = httpx.head(probe_url, timeout=5.0, follow_redirects=True, headers=_HEADERS)
            if head.is_success and _ct_is_feed(head.headers.get("content-type", "")):
                resolved = str(head.url)
                if resolved not in candidates:
                    candidates.append(resolved)
                break  # first working path is enough
        except Exception:
            continue

    return candidates
