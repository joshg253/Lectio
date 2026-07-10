"""Feed-redirector link detection.

Some feeds route entry links through a redirector service (FeedBurner's
feedproxy.google.com / feeds.feedburner.com, FeedsPortal). Those links die
when the service does — feedproxy.google.com is already gone, stranding
years of starred articles on dead URLs. Detection lives here so ingest-time
(Inoreader import), star-time (starred-archive capture), and backfill code
share one definition.
"""
from __future__ import annotations

from urllib.parse import urlparse

REDIRECTOR_HOSTS = frozenset({
    "feedproxy.google.com",
    "feeds.feedburner.com",
    "feedsportal.com",
    "rss.feedsportal.com",
})


def is_redirector_link(url: str) -> bool:
    """True if *url* goes through a known feed-redirector service.

    "/~r/" is FeedBurner's path signature; checking it too covers
    publisher-CNAMEd burner domains (e.g. feeds.arstechnica.com)."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    return parsed.netloc.lower() in REDIRECTOR_HOSTS or "/~r/" in parsed.path


def pick_non_redirector(candidates: list[str]) -> str:
    """First non-redirector URL from *candidates*, else the first non-empty."""
    urls = [u for u in candidates if u]
    for u in urls:
        if not is_redirector_link(u):
            return u
    return urls[0] if urls else ""
