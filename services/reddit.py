"""Reddit feed identity and post conversion, shared by the Redlisten sweep (``services/redlisten.py``).

Reddit RSS is gone, so a Reddit feed is a special type: it is stored under a reddit.com RSS-style URL that is only an identity (never
fetched) and filled with Reddit-compatible listings from Redlisten.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from services import html_sanitize

# ---------------------------------------------------------------------------
# Feed URL helpers
# ---------------------------------------------------------------------------


def is_reddit_feed_url(url: str) -> bool:
    """True if *url* is a Reddit feed URL (old. or www.).

    Exact host suffix match, not substring — "reddit.com.evil.com" must not pass.
    """
    try:
        host = (urlparse(url).netloc or "").lower().rstrip(".")
    except Exception:
        return False
    return host == "reddit.com" or host.endswith(".reddit.com")


_SHORTHAND_RE = re.compile(r"^/?(?:r/(?P<sub>[A-Za-z0-9_]+)|u(?:ser)?/(?P<user>[A-Za-z0-9_-]+))/?$", re.IGNORECASE)
_PAGE_RE = re.compile(r"^/(?:r/(?P<sub>[A-Za-z0-9_]+)|u(?:ser)?/(?P<user>[A-Za-z0-9_-]+))(?:/|$)", re.IGNORECASE)
# What the Redlisten sweep can fill: a subreddit (or a+b list) newest-first, or a user's submissions. Anything else (search, top, a
# single thread, a user's overview, a multireddit) has no equivalent and is reported as unsupported rather than guessed at.
_TARGET_SUB_RE = re.compile(r"^/r/([A-Za-z0-9_]+(?:\+[A-Za-z0-9_]+)*)(?:/new)?/?(?:\.rss)?/?$", re.IGNORECASE)
_TARGET_USER_RE = re.compile(r"^/(?:user|u)/([A-Za-z0-9_-]+)/submitted(?:/new)?/?(?:\.rss)?/?$", re.IGNORECASE)


def feed_target(url: str) -> tuple[str, list[str]] | None:
    """What a stored Reddit feed URL follows: ``("sub", [names])`` or ``("user", [name])``, or None if unsupported."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    # A search or time filter in the query narrows the feed; following the whole subreddit instead would flood it.
    if {"q", "t", "restrict_sr", "flair"} & set(parse_qs(parsed.query)):
        return None
    path = parsed.path or ""
    if m := _TARGET_SUB_RE.match(path):
        return "sub", m.group(1).split("+")
    if m := _TARGET_USER_RE.match(path):
        return "user", [m.group(1)]
    return None


def reddit_feed_url_from_input(text: str) -> str | None:
    """Turn what a user types into Add Feed (``r/name``, ``u/name``, or any reddit.com subreddit/user URL) into the stable identity URL
    Lectio stores a Reddit feed under, or None if it isn't Reddit. Reddit RSS is dead, so this URL is only a key, never fetched."""
    text = (text or "").strip()
    if not text:
        return None
    if "://" not in text and re.match(r"^(?:[\w-]+\.)?reddit\.com/", text, re.IGNORECASE):
        text = "https://" + text
    match = _SHORTHAND_RE.match(text)
    if not match and "://" in text and is_reddit_feed_url(text):
        match = _PAGE_RE.match(urlparse(text).path)
    if not match:
        return None
    if match.group("sub"):
        return f"https://www.reddit.com/r/{match.group('sub')}/.rss"
    return f"https://www.reddit.com/user/{match.group('user')}/submitted/.rss"


def subreddit_from_feed_url(url: str) -> str | None:
    """Extract subreddit name from a Reddit feed URL, or None."""
    m = re.search(r"/r/([A-Za-z0-9_]+)", url)
    return m.group(1) if m else None


def redditor_from_feed_url(url: str) -> str | None:
    """Extract redditor name from a /user/NAME/submitted/ feed URL, or None."""
    m = re.search(r"/user/([A-Za-z0-9_-]+)/submitted", url)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Posts
# ---------------------------------------------------------------------------


def _posts_from_listing(data: dict) -> list[dict]:
    return [child["data"] for child in data.get("data", {}).get("children", []) if child.get("kind") == "t3"]


def _clean_summary(raw: str) -> str:
    """Reddit's ``selftext_html`` is entity-escaped HTML; decode it, then run it through the same allowlist as fetched feed HTML."""
    if not raw:
        return ""
    if "&lt;" in raw and "<" not in raw:
        raw = html.unescape(raw)
    return html_sanitize.sanitize_html(raw)


def post_to_entry(post: dict, feed_url: str) -> dict:
    """Convert a Reddit-style post dict into a reader-compatible entry dict for *feed_url*.

    Entry ``id`` is the post's permalink URL. (The old RSS entries used ``.../r/<sub>/t3_<id>``, so the two never share an id.)
    """
    permalink = "https://www.reddit.com" + post.get("permalink", "")
    url = post.get("url") or permalink
    published_ts = post.get("created_utc")
    published = datetime.fromtimestamp(published_ts, timezone.utc) if published_ts else None
    # Link posts: show the external URL as the main link; self-posts link to the thread.
    link = permalink if post.get("is_self", False) else url
    return {
        "feed_url": feed_url,
        "id": permalink,
        "title": html.unescape(post.get("title", "") or ""),
        "link": link,
        "published": published,
        "summary": _clean_summary(post.get("selftext_html") or ""),
    }
