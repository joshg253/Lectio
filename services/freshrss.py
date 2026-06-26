"""FreshRSS Google Reader-compatible API client for migration into Lectio.

FreshRSS implements the same Google Reader API as Inoreader (subscription/list,
tag/list, stream/contents) but uses password-based ClientLogin auth instead of
OAuth, and accepts a user-supplied instance base URL.

API base: ``{base_url}/api/greader.php``
Auth:     POST ClientLogin → ``GoogleLogin auth=<token>`` header
"""
from __future__ import annotations

from urllib.parse import quote

import httpx

_USER_AGENT = "Lectio/1.0 (+https://github.com/joshg253/Lectio)"
_TIMEOUT = 30

STARRED_STREAM_ID = "user/-/state/com.google/starred"


class AuthError(RuntimeError):
    """Raised when credentials are rejected."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _api_base(instance_url: str) -> str:
    return instance_url.rstrip("/") + "/api/greader.php"


def _auth_header(token: str) -> dict:
    return {"Authorization": f"GoogleLogin auth={token}", "User-Agent": _USER_AGENT}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def login(instance_url: str, username: str, password: str) -> str:
    """ClientLogin: return the auth token string or raise AuthError / RuntimeError."""
    url = _api_base(instance_url) + "/accounts/ClientLogin"
    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.post(
            url,
            data={"Email": username, "Passwd": password},
            headers={"User-Agent": _USER_AGENT},
        )
    if resp.status_code in (401, 403):
        raise AuthError("Invalid FreshRSS credentials")
    if resp.status_code != 200:
        raise RuntimeError(f"ClientLogin failed: HTTP {resp.status_code}: {resp.text[:200]}")
    for line in resp.text.splitlines():
        if line.startswith("Auth="):
            return line[5:].strip()
    raise RuntimeError(f"ClientLogin response missing Auth token: {resp.text[:200]}")


def test_connection(instance_url: str, username: str, password: str) -> dict:
    """Return ``{username}`` on success, raise AuthError / RuntimeError."""
    token = login(instance_url, username, password)
    base = _api_base(instance_url)
    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.get(
            base + "/reader/api/0/user-info",
            params={"output": "json"},
            headers=_auth_header(token),
        )
    if resp.status_code == 401:
        raise AuthError("Token rejected by user-info endpoint")
    if resp.status_code != 200:
        return {"username": username}
    data = resp.json()
    return {"username": data.get("userName") or username}


# ---------------------------------------------------------------------------
# Data fetchers (return raw API dicts — normalisation happens in main.py)
# ---------------------------------------------------------------------------

def get_subscriptions(instance_url: str, token: str) -> list[dict]:
    """Return subscription list. Each has ``feed_url``, ``title``, ``categories``."""
    base = _api_base(instance_url)
    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.get(
            base + "/reader/api/0/subscription/list",
            params={"output": "json"},
            headers=_auth_header(token),
        )
    if resp.status_code == 401:
        raise AuthError("Token expired")
    resp.raise_for_status()
    subs = resp.json().get("subscriptions", [])
    # Normalise the feed URL from the "id" field (format: "feed/<url>")
    for sub in subs:
        raw_id = sub.get("id", "")
        sub["feed_url"] = raw_id[len("feed/"):] if raw_id.startswith("feed/") else raw_id
    return subs


def get_tags(instance_url: str, token: str) -> list[dict]:
    """Return tag/label list. Each has an ``id`` field."""
    base = _api_base(instance_url)
    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.get(
            base + "/reader/api/0/tag/list",
            params={"output": "json"},
            headers=_auth_header(token),
        )
    if resp.status_code == 401:
        raise AuthError("Token expired")
    resp.raise_for_status()
    return resp.json().get("tags", [])


def get_stream_contents(
    instance_url: str,
    token: str,
    stream_id: str,
    continuation: str | None = None,
    n: int = 100,
) -> tuple[list[dict], str | None]:
    """Fetch one page of a stream. Returns (items, next_continuation_or_None)."""
    base = _api_base(instance_url)
    params: dict = {"n": n, "output": "json"}
    if continuation:
        params["c"] = continuation
    url = base + f"/reader/api/0/stream/contents/{quote(stream_id, safe='')}"
    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.get(url, params=params, headers=_auth_header(token))
    if resp.status_code == 401:
        raise AuthError("Token expired")
    resp.raise_for_status()
    data = resp.json()
    return data.get("items", []), data.get("continuation") or None


# ---------------------------------------------------------------------------
# Label helpers (same scheme as Inoreader)
# ---------------------------------------------------------------------------

def label_name_from_tag_id(tag_id: str) -> str | None:
    """Return label name from ``user/.../label/NAME``, or None."""
    parts = tag_id.split("/label/")
    return parts[1] if len(parts) == 2 else None


def label_is_tag(label_name: str) -> bool:
    """Lowercase label → article tag; Title Case → folder (skip for tag import)."""
    return label_name == label_name.lower()


def label_stream_id(label_name: str) -> str:
    return f"user/-/label/{label_name}"


# ---------------------------------------------------------------------------
# Item normaliser (Google Reader item dict → import-item dict)
# ---------------------------------------------------------------------------

def normalize_item(item: dict, folder: str = "") -> dict:
    """Convert a Google Reader stream item to a normalized import-item dict."""
    canonical = item.get("canonical") or []
    entry_url = canonical[0].get("href", "") if canonical else ""
    origin = item.get("origin") or {}
    raw_stream = origin.get("streamId", "")
    feed_url = raw_stream[len("feed/"):] if raw_stream.startswith("feed/") else raw_stream
    summary = item.get("summary") or {}
    content = summary.get("content", "") or (item.get("content") or {}).get("content", "")
    cats = item.get("categories") or []
    labels = [
        name
        for c in cats
        if (name := label_name_from_tag_id(c)) and label_is_tag(name)
    ]
    return {
        "url": entry_url,
        "title": item.get("title", ""),
        "published": item.get("published"),
        "feed_url": feed_url,
        "feed_title": origin.get("title", ""),
        "content": content,
        "starred": False,
        "tags": labels,
        "folder": folder,
    }
