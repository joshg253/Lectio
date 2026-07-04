"""Bluesky (AT Protocol) image recovery for bsky.app RSS feeds.

Bluesky's per-profile RSS (``https://bsky.app/profile/<did>/rss``) is text-only —
posts carry no images, and content-labeled posts (e.g. adult) expose no og:image
on the web page either. But the post's images live in its record and are served
from the public CDN (``cdn.bsky.app``); the content label only gates the official
app UI, not the API or CDN. So we fetch the images straight from the public
AT Protocol API using the post's ``at://`` URI (which the RSS feed stores as the
entry id/guid) and surface them as the entry's lead image + article content.

No auth is required and no label is honored at this layer — the feed subscription
is the user's explicit opt-in to that account's posts.
"""
from __future__ import annotations

import logging
import re
import threading
import time

import httpx

LOGGER = logging.getLogger(__name__)

# Per-profile RSS feed: https://bsky.app/profile/<did>/rss (DID or handle).
_BSKY_FEED_RE = re.compile(r"^https?://bsky\.app/profile/[^/]+/rss/?$", re.IGNORECASE)

_API_GET_POSTS = "https://public.api.bsky.app/xrpc/app.bsky.feed.getPosts"
_USER_AGENT = "Lectio/1.0 (+https://github.com/joshg253/Lectio)"
_HTTP_TIMEOUT = 15.0

# at:// URI -> (fetched_at, [image_urls]). Small in-memory TTL cache so list
# thumbnails don't re-hit the API for every render.
_cache: dict[str, tuple[float, list[str]]] = {}
_cache_lock = threading.Lock()
_TTL_SECONDS = 3600.0


def is_bsky_feed(feed_url: str | None) -> bool:
    """True for a Bluesky per-profile RSS feed URL."""
    return bool(feed_url and _BSKY_FEED_RE.match(feed_url))


def _images_from_embed(embed: object, out: list[str]) -> None:
    """Collect fullsize CDN image URLs from a post-view embed (recursively for
    recordWithMedia). Prefers ``fullsize``, falls back to ``thumb``."""
    if not isinstance(embed, dict):
        return
    etype = str(embed.get("$type") or "")
    if etype.startswith("app.bsky.embed.images"):
        for img in embed.get("images", []) or []:
            if isinstance(img, dict):
                url = img.get("fullsize") or img.get("thumb")
                if url:
                    out.append(str(url))
    elif etype.startswith("app.bsky.embed.recordWithMedia"):
        _images_from_embed(embed.get("media"), out)


def _images_from_post(post: dict) -> list[str]:
    out: list[str] = []
    _images_from_embed(post.get("embed"), out)
    # De-dupe while preserving order.
    seen: set[str] = set()
    return [u for u in out if not (u in seen or seen.add(u))]


def fetch_post_images(at_uri: str | None) -> list[str]:
    """Return the CDN image URLs for a Bluesky post given its ``at://`` URI.

    Cached in-memory for an hour. Returns [] on any error or for a post with no
    images (e.g. text-only or external-link embeds)."""
    if not at_uri or not at_uri.startswith("at://"):
        return []
    now = time.time()
    with _cache_lock:
        hit = _cache.get(at_uri)
        if hit and now - hit[0] < _TTL_SECONDS:
            return hit[1]
    urls: list[str] = []
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT, headers={"User-Agent": _USER_AGENT}) as client:
            resp = client.get(_API_GET_POSTS, params={"uris": at_uri})
            resp.raise_for_status()
            posts = resp.json().get("posts", [])
            if posts:
                urls = _images_from_post(posts[0])
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("[bsky] getPosts failed for %s: %s", at_uri, exc)
        urls = []
    with _cache_lock:
        _cache[at_uri] = (now, urls)
    return urls
