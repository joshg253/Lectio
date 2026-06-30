"""Tiny Tiny RSS JSON-RPC API client for migration into Lectio.

tt-rss exposes a JSON-RPC API at ``{base_url}/api/``.
Auth: username + password → session_id token (per-session, not OAuth).
No rate limits (self-hosted).

Key virtual feed IDs (used in getHeadlines):
  -1  Starred articles
  -2  Published articles
  -3  Fresh articles
  -4  All articles
"""
from __future__ import annotations

import httpx

from services.url_guard import ensure_safe_outbound_url

_USER_AGENT = "Lectio/1.0 (+https://github.com/joshg253/Lectio)"
_TIMEOUT = 30


class AuthError(RuntimeError):
    """Raised when credentials are rejected by tt-rss."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _api_url(base_url: str) -> str:
    # SSRF guard: the single JSON-RPC endpoint all requests POST to. Validating
    # the user-supplied host here covers every op. The client doesn't follow
    # redirects (httpx default), so the host can't be bounced internally.
    return ensure_safe_outbound_url(base_url.rstrip("/") + "/api/")


def _rpc(client: httpx.Client, url: str, sid: str | None, op: str, **params) -> dict:
    """POST one JSON-RPC call; return the ``content`` value or raise."""
    body: dict = {"op": op, **params}
    if sid:
        body["sid"] = sid
    resp = client.post(url, json=body, headers={"User-Agent": _USER_AGENT})
    resp.raise_for_status()
    data = resp.json()
    status = data.get("status", -1)
    content = data.get("content", {})
    if status != 0:
        error = content.get("error", "") if isinstance(content, dict) else str(content)
        if "LOGIN_ERROR" in error or "NOT_LOGGED_IN" in error:
            raise AuthError(f"tt-rss auth error: {error}")
        raise RuntimeError(f"tt-rss API error (op={op}): {error}")
    return content


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def login(base_url: str, username: str, password: str) -> str:
    """Return a session_id string or raise AuthError / RuntimeError."""
    url = _api_url(base_url)
    with httpx.Client(timeout=_TIMEOUT) as client:
        content = _rpc(client, url, None, "login", user=username, password=password)
    session_id = content.get("session_id", "") if isinstance(content, dict) else ""
    if not session_id:
        raise AuthError("tt-rss returned no session_id")
    return session_id


def test_connection(base_url: str, username: str, password: str) -> dict:
    """Return ``{version}`` on success, raise AuthError / RuntimeError."""
    sid = login(base_url, username, password)
    url = _api_url(base_url)
    with httpx.Client(timeout=_TIMEOUT) as client:
        content = _rpc(client, url, sid, "getVersion")
    version = content.get("version", "") if isinstance(content, dict) else ""
    return {"version": version}


# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------

def get_categories(base_url: str, sid: str) -> list[dict]:
    """Return user-defined categories (folders). Skips virtual negative-ID cats."""
    url = _api_url(base_url)
    with httpx.Client(timeout=_TIMEOUT) as client:
        content = _rpc(client, url, sid, "getCategories", include_nested=False, include_empty=True)
    cats = content if isinstance(content, list) else []
    return [c for c in cats if isinstance(c.get("id"), int) and c["id"] > 0]


def get_all_feeds(base_url: str, sid: str) -> list[dict]:
    """Return all subscribed feeds as ``{feed_url, title, cat_id}`` dicts."""
    url = _api_url(base_url)
    with httpx.Client(timeout=_TIMEOUT) as client:
        # cat_id=-3 with include_nested fetches all feeds from all categories.
        content = _rpc(client, url, sid, "getFeeds", cat_id=-3, include_nested=True, limit=0)
    feeds = content if isinstance(content, list) else []
    # Skip virtual feeds (negative IDs).
    return [
        {
            "feed_url": f.get("feed_url", ""),
            "title": f.get("title", ""),
            "cat_id": f.get("cat_id", 0),
        }
        for f in feeds
        if isinstance(f.get("id"), int) and f["id"] > 0 and f.get("feed_url")
    ]


def get_starred_headlines(
    base_url: str,
    sid: str,
    limit: int = 200,
    skip: int = 0,
) -> list[dict]:
    """Return up to ``limit`` starred/marked headlines starting at ``skip``."""
    url = _api_url(base_url)
    with httpx.Client(timeout=_TIMEOUT) as client:
        content = _rpc(
            client, url, sid,
            "getHeadlines",
            feed_id=-1,          # -1 = Starred articles virtual feed
            view_mode="all_articles",
            show_content=True,
            include_attachments=False,
            limit=limit,
            skip=skip,
        )
    headlines = content if isinstance(content, list) else []
    return headlines


def get_labels(base_url: str, sid: str) -> list[dict]:
    """Return user-defined labels as ``{id, caption}`` dicts."""
    url = _api_url(base_url)
    with httpx.Client(timeout=_TIMEOUT) as client:
        try:
            content = _rpc(client, url, sid, "getLabels")
        except RuntimeError:
            return []
    labels = content if isinstance(content, list) else []
    return [{"id": lb.get("id"), "caption": lb.get("caption", "")} for lb in labels]


# ---------------------------------------------------------------------------
# Item normaliser
# ---------------------------------------------------------------------------

def normalize_headline(
    headline: dict,
    feed_info_map: dict[int, dict],
    cat_name_map: dict[int, str],
) -> dict:
    """Convert a tt-rss headline to a normalized import-item dict.

    ``feed_info_map``: feed_id → {feed_url, title, cat_id}
    ``cat_name_map``:  cat_id  → folder_name
    """
    feed_id = headline.get("feed_id") or 0
    feed_info = feed_info_map.get(feed_id, {})
    feed_url = feed_info.get("feed_url", "")
    cat_id = feed_info.get("cat_id", 0)
    folder = cat_name_map.get(cat_id, "")

    # Labels: [[id, caption, fg, bg, checked], ...]
    raw_labels = headline.get("labels") or []
    tags = [
        row[1].lower()
        for row in raw_labels
        if isinstance(row, list) and len(row) >= 2 and row[1]
    ]

    published = headline.get("updated") or headline.get("published")

    return {
        "url": headline.get("link", "") or headline.get("guid", ""),
        "title": headline.get("title", ""),
        "published": int(published) if published else None,
        "feed_url": feed_url,
        "feed_title": feed_info.get("title", "") or headline.get("feed_title", ""),
        "content": headline.get("content", ""),
        "starred": True,
        "tags": tags,
        "folder": folder,
    }
