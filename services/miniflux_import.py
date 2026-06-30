"""Miniflux REST API client for one-shot migration into Lectio.

This is the *source* client (migrating FROM a Miniflux instance),
distinct from services/miniflux.py which is the Miniflux-API *server*
compatibility layer (letting other apps talk TO Lectio as Miniflux).

Auth: API token sent as ``X-Auth-Token`` header.
No rate limits (self-hosted), so the full import runs in a single pass.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from services.url_guard import ensure_safe_outbound_url

_USER_AGENT = "Lectio/1.0 (+https://github.com/joshg253/Lectio)"
_TIMEOUT = 30


class AuthError(RuntimeError):
    """Raised when the API token is rejected (HTTP 401)."""


def _api_url(base_url: str, path: str) -> str:
    # SSRF guard: all requests build their URL through here, so validating the
    # user-supplied host blocks private/loopback targets. Clients don't follow
    # redirects (httpx default), closing the redirect-to-internal bypass.
    return ensure_safe_outbound_url(base_url.rstrip("/") + path)


def _headers(token: str) -> dict:
    return {"X-Auth-Token": token, "User-Agent": _USER_AGENT}


# ---------------------------------------------------------------------------
# Connection test
# ---------------------------------------------------------------------------

def test_connection(base_url: str, token: str) -> dict:
    """Return ``{username}`` on success, raise AuthError / RuntimeError on failure."""
    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.get(_api_url(base_url, "/v1/me"), headers=_headers(token))
    if resp.status_code == 401:
        raise AuthError("Invalid API token")
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    return {"username": data.get("username", "")}


# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------

def get_feeds(base_url: str, token: str) -> list[dict]:
    """Return all subscribed feeds as ``{feed_url, title, folder}`` dicts."""
    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.get(_api_url(base_url, "/v1/feeds"), headers=_headers(token))
    if resp.status_code == 401:
        raise AuthError("Invalid API token")
    resp.raise_for_status()
    out = []
    for f in resp.json():
        cat = f.get("category") or {}
        out.append({
            "feed_url": f.get("feed_url", ""),
            "title": f.get("title", ""),
            "folder": cat.get("title", ""),
        })
    return out


def get_starred_entries(base_url: str, token: str) -> list[dict]:
    """Page through all starred entries and return normalized import-item dicts."""
    items: list[dict] = []
    limit = 100
    offset = 0
    with httpx.Client(timeout=_TIMEOUT) as client:
        while True:
            resp = client.get(
                _api_url(base_url, "/v1/entries"),
                headers=_headers(token),
                params={"starred": "true", "limit": limit, "offset": offset, "direction": "desc"},
            )
            if resp.status_code == 401:
                raise AuthError("Invalid API token")
            resp.raise_for_status()
            data = resp.json()
            entries = data.get("entries") or []
            for e in entries:
                feed = e.get("feed") or {}
                published = _parse_iso(e.get("published_at") or "")
                cat = (feed.get("category") or {})
                items.append({
                    "url": e.get("url", ""),
                    "title": e.get("title", ""),
                    "published": published,
                    "feed_url": feed.get("feed_url", ""),
                    "feed_title": feed.get("title", ""),
                    "content": e.get("content", ""),
                    "starred": True,
                    "tags": [t for t in (e.get("tags") or []) if t],
                    "folder": cat.get("title", ""),
                })
            if len(entries) < limit:
                break
            offset += limit
    return items


def _parse_iso(s: str) -> int | None:
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None
