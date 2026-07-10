"""Inoreader OAuth2 client and import helpers.

Uses Inoreader's Google Reader–compatible API:
  https://www.inoreader.com/developers/

Auth: OAuth 2.0 authorization_code flow. Client credentials go in the token
POST body (not HTTP Basic like Pinterest). API calls use Bearer token only.

Rate limits: two zones tracked via response headers.
  Zone 1: daily quota (free Pro = 250 calls/day)
  Zone 2: per-minute burst limit
Both are surfaced in the import checkpoint so the drip strategy can stay
within limits automatically.
"""
from __future__ import annotations

from services import link_canonical

from urllib.parse import urlencode, quote

import httpx

_AUTHORIZE_URL = "https://www.inoreader.com/oauth2/auth"
_TOKEN_URL = "https://www.inoreader.com/oauth2/token"
_API_BASE = "https://www.inoreader.com/reader/api/0"
_SCOPE = "read write"
_USER_AGENT = "Lectio/1.0 (+https://github.com/joshg253/Lectio)"
_TIMEOUT = 45


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class QuotaExceeded(RuntimeError):
    """Raised when Inoreader returns HTTP 429 (rate limit / quota exhausted)."""
    def __init__(self, msg: str, rate_limits: dict | None = None):
        super().__init__(msg)
        self.rate_limits = rate_limits or {}


# ---------------------------------------------------------------------------
# OAuth helpers
# ---------------------------------------------------------------------------

def authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Build the Inoreader consent-screen URL."""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": _SCOPE,
        "state": state,
    }
    return f"{_AUTHORIZE_URL}?{urlencode(params)}"


def _post_token(payload: dict, what: str) -> dict:
    with httpx.Client(timeout=_TIMEOUT, headers={"User-Agent": _USER_AGENT}) as client:
        resp = client.post(
            _TOKEN_URL,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    data = resp.json() if "json" in resp.headers.get("content-type", "") else {}
    if resp.status_code == 200 and data.get("access_token"):
        return data
    raise RuntimeError(f"{what} failed: HTTP {resp.status_code}: {resp.text[:200]}")


def exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    """Exchange an authorization code for access + refresh tokens."""
    return _post_token({
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
    }, "token exchange")


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> dict:
    """Refresh an expired access token."""
    return _post_token({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    }, "token refresh")


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}", "User-Agent": _USER_AGENT}


def _parse_rate_limits(resp_headers) -> dict:
    """Extract zone1/zone2 quota info from response headers."""
    def _int(v: str | None) -> int | None:
        try:
            return int(v) if v is not None else None
        except ValueError:
            return None
    return {
        "z1_limit": _int(resp_headers.get("x-reader-zone1-limit")),
        "z1_usage": _int(resp_headers.get("x-reader-zone1-usage")),
        "z2_limit": _int(resp_headers.get("x-reader-zone2-limit")),
        "z2_usage": _int(resp_headers.get("x-reader-zone2-usage")),
        "reset_after": resp_headers.get("x-reader-limits-reset-after"),
    }


def _z1_remaining(rl: dict) -> int | None:
    lim, usage = rl.get("z1_limit"), rl.get("z1_usage")
    if lim is None or usage is None:
        return None
    return lim - usage


def get_user_info(access_token: str) -> dict:
    """Return Inoreader user profile dict."""
    with httpx.Client(timeout=_TIMEOUT, headers=_headers(access_token)) as client:
        resp = client.get(f"{_API_BASE}/user-info")
    if resp.status_code != 200:
        raise RuntimeError(f"user-info failed: HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def get_subscriptions(access_token: str) -> tuple[list[dict], dict]:
    """Return (subscriptions_list, rate_limits). Each sub has at minimum 'url', 'title'."""
    with httpx.Client(timeout=_TIMEOUT, headers=_headers(access_token)) as client:
        resp = client.get(f"{_API_BASE}/subscription/list", params={"output": "json"})
    rl = _parse_rate_limits(resp.headers)
    if resp.status_code == 429:
        raise QuotaExceeded("Inoreader quota exhausted", rate_limits=rl)
    if resp.status_code != 200:
        raise RuntimeError(f"subscription/list failed: HTTP {resp.status_code}: {resp.text[:200]}")
    subs = resp.json().get("subscriptions", [])
    # Normalise: extract the feed URL from the 'id' field (format: "feed/<url>")
    for sub in subs:
        raw_id = sub.get("id", "")
        if raw_id.startswith("feed/"):
            sub["feed_url"] = raw_id[len("feed/"):]
        else:
            sub["feed_url"] = raw_id
    return subs, rl


def get_tags(access_token: str) -> tuple[list[dict], dict]:
    """Return (tags_list, rate_limits). Each tag has an 'id' field."""
    with httpx.Client(timeout=_TIMEOUT, headers=_headers(access_token)) as client:
        resp = client.get(f"{_API_BASE}/tag/list", params={"output": "json"})
    rl = _parse_rate_limits(resp.headers)
    if resp.status_code == 429:
        raise QuotaExceeded("Inoreader quota exhausted", rate_limits=rl)
    if resp.status_code != 200:
        raise RuntimeError(f"tag/list failed: HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json().get("tags", []), rl


def get_stream_contents(
    access_token: str,
    stream_id: str,
    continuation: str | None = None,
    n: int = 100,
    ot: int | None = None,
) -> tuple[list[dict], str | None, dict]:
    """Page through a stream. Returns (items, next_continuation_or_None, rate_limits)."""
    params: dict = {"n": n, "output": "json"}
    if continuation:
        params["c"] = continuation
    if ot is not None:
        params["ot"] = ot
    url = f"{_API_BASE}/stream/contents/{quote(stream_id, safe='')}"
    with httpx.Client(timeout=_TIMEOUT, headers=_headers(access_token)) as client:
        resp = client.get(url, params=params)
    rl = _parse_rate_limits(resp.headers)
    if resp.status_code == 429:
        raise QuotaExceeded("Inoreader quota exhausted", rate_limits=rl)
    if resp.status_code != 200:
        raise RuntimeError(f"stream/contents failed: HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    return data.get("items", []), data.get("continuation") or None, rl


def edit_tag_remove(access_token: str, item_ids: list[str], tag: str) -> dict:
    """Remove *tag* from one or more Inoreader items in one POST. Returns rate_limits."""
    if not item_ids:
        return {}
    params = [("r", tag)] + [("i", iid) for iid in item_ids]
    with httpx.Client(timeout=_TIMEOUT, headers=_headers(access_token)) as client:
        resp = client.post(
            f"{_API_BASE}/edit-tag",
            data=params,
        )
    rl = _parse_rate_limits(resp.headers)
    if resp.status_code == 429:
        raise QuotaExceeded("Inoreader quota exhausted", rate_limits=rl)
    if resp.status_code not in (200, 204):
        raise RuntimeError(f"edit-tag failed: HTTP {resp.status_code}: {resp.text[:200]}")
    return rl


# ---------------------------------------------------------------------------
# Label / stream ID helpers
# ---------------------------------------------------------------------------

STARRED_STREAM_ID = "user/-/state/com.google/starred"
READ_STREAM_ID = "user/-/state/com.google/read"


def label_name_from_tag_id(tag_id: str) -> str | None:
    """Return the label name from 'user/.../label/NAME', or None if not a label."""
    parts = tag_id.split("/label/")
    return parts[1] if len(parts) == 2 else None


def label_stream_id(label_name: str) -> str:
    return f"user/-/label/{label_name}"


def label_is_tag(label_name: str) -> bool:
    """All-lowercase label → treat as article tag; Title Case → user folder name."""
    return label_name == label_name.lower()


# ---------------------------------------------------------------------------
# JSON file import (Path B — no API calls needed)
# ---------------------------------------------------------------------------

def _coerce_published(item: dict) -> int | None:
    """Best-available publish timestamp (Unix seconds) for a native item.

    Falls back from the item's own ``published`` to Inoreader's ``crawlTimeMsec``
    (milliseconds) and ``timestampUsec`` (microseconds) so date-less entries
    still get a real age instead of defaulting to import time downstream."""
    pub = item.get("published")
    if pub:
        try:
            return int(pub)
        except (TypeError, ValueError):
            pass
    crawl_ms = item.get("crawlTimeMsec")
    if crawl_ms:
        try:
            return int(crawl_ms) // 1000
        except (TypeError, ValueError):
            pass
    ts_usec = item.get("timestampUsec")
    if ts_usec:
        try:
            return int(ts_usec) // 1_000_000
        except (TypeError, ValueError):
            pass
    return None


def parse_export_json(data) -> list[dict]:
    """Parse items into normalised records.

    Accepts three formats:
    - ExportTool / native Inoreader export: a plain ``list`` of item dicts with
      ``categories`` paths (``user/ID/label/NAME``) and ``canonical`` URL objects.
    - Native Inoreader export ZIP: a ``dict`` with an ``"items"`` key containing
      the same item structure as above.
    - JSON Feed (jsonfeed.org/version/1): a ``dict`` with ``version`` starting
      with ``https://jsonfeed.org/``. Items have a flat ``url`` string, a ``tags``
      list of plain label names, and no per-item feed URL.

    Each returned record:
      url          — article canonical URL
      title        — article title
      published    — Unix timestamp (int) or None
      feed_url     — feed subscription URL, or ``""`` if unknown (JSON Feed)
      feed_title   — feed display name
      content      — article HTML (may be empty)
      starred      — True if starred
      labels       — list[str] of user label names
      item_id      — Inoreader item ID
    """
    if isinstance(data, dict) and data.get("version", "").startswith("https://jsonfeed.org/"):
        return _parse_jsonfeed(data)

    is_native = isinstance(data, dict)
    if is_native:
        raw_items: list = data.get("items", [])
    elif isinstance(data, list):
        raw_items = data
    else:
        return []

    out = []
    for item in raw_items:
        cats = item.get("categories", [])
        # Prefer a non-redirector URL: for FeedBurner-era feeds the canonical
        # slot often carries the (now-dead) feedproxy link while alternate has
        # the real article URL — or vice versa. Either way, pick whichever
        # candidate isn't a redirector (services.link_canonical).
        canonical = item.get("canonical") or []
        alternate = item.get("alternate") or []
        url = link_canonical.pick_non_redirector([
            c.get("href", "") for c in (list(canonical) + list(alternate)) if isinstance(c, dict)
        ])
        origin = item.get("origin") or {}
        raw_stream = origin.get("streamId", "")
        feed_url = raw_stream[len("feed/"):] if raw_stream.startswith("feed/") else raw_stream
        # Starred: only the native Inoreader export (dict format, starred*.json) is the
        # source of truth for starred state. It uses a Unix-timestamp ``starred`` field.
        # ExportTool files (list format) carry starred state in categories too, but we
        # intentionally ignore it — starred.json is authoritative.
        if is_native:
            starred = bool(item.get("starred")) or any(
                ("state/com.google/like" in c or "state/com.google/starred" in c)
                for c in cats
            )
        else:
            starred = False
        labels = []
        for c in cats:
            name = label_name_from_tag_id(c)
            if name:
                labels.append(name)
        summary = item.get("summary") or {}
        out.append({
            "url": url,
            "title": item.get("title", ""),
            # Prefer the item's own published date; fall back to Inoreader's crawl
            # time so entries that omit <pubDate> still carry a real timestamp and
            # sort by true age instead of clustering at import time.
            "published": _coerce_published(item),
            "feed_url": feed_url,
            "feed_title": origin.get("title", ""),
            "content": summary.get("content", ""),
            "starred": starred,
            "labels": labels,
            "item_id": item.get("id", ""),
        })
    return out


def _parse_jsonfeed(data: dict) -> list[dict]:
    """Parse a jsonfeed.org/version/1 export from Inoreader."""
    from datetime import datetime, timezone
    out = []
    for item in data.get("items", []):
        pub = None
        raw_pub = item.get("date_published") or item.get("date_modified")
        if raw_pub:
            try:
                pub = int(datetime.fromisoformat(raw_pub.replace("Z", "+00:00")).timestamp())
            except Exception:
                pass
        out.append({
            "url": item.get("url", ""),
            "title": item.get("title", ""),
            "published": pub,
            "feed_url": "",  # not available per-item in JSON Feed
            "feed_title": "",
            "content": item.get("content_html", "") or item.get("content_text", ""),
            "starred": False,  # JSON Feed exports are label streams, not starred
            "labels": list(item.get("tags") or []),
            "item_id": item.get("id", ""),
        })
    return out


# ---------------------------------------------------------------------------
# Rate-limit helpers for callers
# ---------------------------------------------------------------------------

def z1_remaining(rl: dict) -> int | None:
    """Zone-1 calls remaining, or None if headers were absent."""
    return _z1_remaining(rl)
