"""Reddit OAuth2 client and feed-sync helpers.

Uses Reddit's OAuth2 API (authorization_code flow) for both authenticated
feed reading (60 req/min vs anonymous limits) and link submission.

Auth: OAuth 2.0 authorization_code flow. Client credentials use HTTP Basic
auth on the token endpoint (unlike Inoreader which uses the POST body).
Access tokens expire after 1 hour; refresh tokens are permanent for
``duration=permanent`` grants.

Reddit API rules:
  - Unique User-Agent per app: ``platform:app_id:version (by /u/username)``
  - Respect ``Retry-After`` header on 429 responses.
  - No more than 60 OAuth requests per minute per token.
"""
from __future__ import annotations

import base64
import re
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx

_AUTHORIZE_URL = "https://www.reddit.com/api/v1/authorize"
_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_API_BASE = "https://oauth.reddit.com"
_SCOPE = "identity read submit"
_USER_AGENT = "Lectio/1.0 (+https://github.com/joshg253/Lectio)"
_TIMEOUT = 30


# ---------------------------------------------------------------------------
# OAuth helpers
# ---------------------------------------------------------------------------

def authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Build the Reddit consent-screen URL."""
    params = {
        "client_id": client_id,
        "response_type": "code",
        "state": state,
        "redirect_uri": redirect_uri,
        "duration": "permanent",
        "scope": _SCOPE,
    }
    return f"{_AUTHORIZE_URL}?{urlencode(params)}"


def _basic_auth(client_id: str, client_secret: str) -> str:
    return "Basic " + base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()


def _post_token(client_id: str, client_secret: str, payload: dict, what: str) -> dict:
    headers = {
        "User-Agent": _USER_AGENT,
        "Authorization": _basic_auth(client_id, client_secret),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.post(_TOKEN_URL, data=payload, headers=headers)
    data = resp.json() if "json" in resp.headers.get("content-type", "") else {}
    if resp.status_code == 200 and data.get("access_token"):
        return data
    raise RuntimeError(f"{what} failed: HTTP {resp.status_code}: {resp.text[:200]}")


def exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    """Exchange an authorization code for access + refresh tokens."""
    return _post_token(client_id, client_secret, {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }, "token exchange")


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> dict:
    """Refresh an expired access token."""
    return _post_token(client_id, client_secret, {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }, "token refresh")


def _api_headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}", "User-Agent": _USER_AGENT}


def get_me(access_token: str) -> dict:
    """Return the authenticated user's profile (includes ``name``)."""
    with httpx.Client(timeout=_TIMEOUT, headers=_api_headers(access_token)) as client:
        resp = client.get(f"{_API_BASE}/api/v1/me")
    if resp.status_code != 200:
        raise RuntimeError(f"get_me failed: HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()


# ---------------------------------------------------------------------------
# Feed URL helpers
# ---------------------------------------------------------------------------

def is_reddit_feed_url(url: str) -> bool:
    """True if *url* is a Reddit feed URL (old. or www.)."""
    return "reddit.com" in url


def subreddit_from_feed_url(url: str) -> str | None:
    """Extract subreddit name from a Reddit feed URL, or None."""
    m = re.search(r"/r/([A-Za-z0-9_]+)", url)
    return m.group(1) if m else None


def redditor_from_feed_url(url: str) -> str | None:
    """Extract redditor name from a /user/NAME/submitted/ feed URL, or None."""
    m = re.search(r"/user/([A-Za-z0-9_-]+)/submitted", url)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Feed fetching
# ---------------------------------------------------------------------------

def _posts_from_listing(data: dict) -> list[dict]:
    return [child["data"] for child in data.get("data", {}).get("children", [])
            if child.get("kind") == "t3"]


def get_subreddit_new(access_token: str, subreddit: str, limit: int = 100) -> list[dict]:
    """Fetch the newest posts from a subreddit. Returns raw Reddit post dicts."""
    with httpx.Client(timeout=_TIMEOUT, headers=_api_headers(access_token)) as client:
        resp = client.get(f"{_API_BASE}/r/{subreddit}/new", params={"limit": limit})
    if resp.status_code == 403:
        raise RuntimeError(f"r/{subreddit} is private or banned")
    if resp.status_code != 200:
        raise RuntimeError(f"subreddit new failed: HTTP {resp.status_code}: {resp.text[:200]}")
    return _posts_from_listing(resp.json())


def get_user_submitted(access_token: str, redditor: str, limit: int = 100) -> list[dict]:
    """Fetch the newest submissions by a user."""
    with httpx.Client(timeout=_TIMEOUT, headers=_api_headers(access_token)) as client:
        resp = client.get(f"{_API_BASE}/user/{redditor}/submitted", params={"limit": limit, "sort": "new"})
    if resp.status_code != 200:
        raise RuntimeError(f"user submitted failed: HTTP {resp.status_code}: {resp.text[:200]}")
    return _posts_from_listing(resp.json())


def fetch_reddit_feed_entries(access_token: str, feed_url: str) -> list[dict]:
    """Fetch posts for *feed_url* via the Reddit API and return reader-compatible entry dicts.

    Entry ``id`` is set to the post's canonical permalink URL, which matches
    the ``<guid>`` Reddit publishes in its RSS feeds — so entries synthesised
    here deduplicate cleanly against any already-stored RSS entries.
    """
    subreddit = subreddit_from_feed_url(feed_url)
    redditor = redditor_from_feed_url(feed_url)
    if subreddit:
        posts = get_subreddit_new(access_token, subreddit)
    elif redditor:
        posts = get_user_submitted(access_token, redditor)
    else:
        return []

    entries = []
    for post in posts:
        permalink = "https://www.reddit.com" + post.get("permalink", "")
        url = post.get("url") or permalink
        published_ts = post.get("created_utc")
        published = datetime.fromtimestamp(published_ts, timezone.utc) if published_ts else None
        selftext_html = post.get("selftext_html") or ""
        # Link posts: show the external URL as the main link; self-posts link to the thread.
        is_self = post.get("is_self", False)
        link = permalink if is_self else url
        entries.append({
            "feed_url": feed_url,
            "id": permalink,
            "title": post.get("title", ""),
            "link": link,
            "published": published,
            "summary": selftext_html,
        })
    return entries


# ---------------------------------------------------------------------------
# Feed sync
# ---------------------------------------------------------------------------

def refresh_all_reddit_feeds(
    meta_conn,
    reader,
    access_token: str,
    max_feeds: int = 30,
) -> int:
    """Fetch new posts for up to *max_feeds* Reddit subscriptions and add them to *reader*.

    Feeds are processed oldest-synced-first so all subscriptions get attention
    across multiple scheduler ticks. Returns the number of new entries added.
    """
    import sqlite3

    rows = meta_conn.execute(
        """
        SELECT DISTINCT ff.feed_url
        FROM folder_feeds ff
        WHERE ff.feed_url LIKE '%reddit.com%'
        ORDER BY COALESCE(
            (SELECT value FROM app_settings WHERE key = 'reddit_last_sync:' || ff.feed_url),
            ''
        ) ASC
        LIMIT ?
        """,
        (max_feeds,),
    ).fetchall()

    added = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    for row in rows:
        feed_url = row[0] if isinstance(row, (tuple, list)) else row["feed_url"]
        try:
            entries = fetch_reddit_feed_entries(access_token, feed_url)
            for e in entries:
                try:
                    reader.add_entry(e)
                    added += 1
                except Exception:
                    pass  # EntryExistsError — already in DB
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "[reddit] failed to sync %s: %s", feed_url, exc
            )
        meta_conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
            (f"reddit_last_sync:{feed_url}", now_iso),
        )
    if rows:
        meta_conn.commit()
    return added


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------

def submit_link(access_token: str, subreddit: str, title: str, url: str) -> dict:
    """Submit a link post to *subreddit*. Returns the Reddit API response dict."""
    payload = {
        "kind": "link",
        "sr": subreddit.lstrip("r/").strip(),
        "title": title[:300],
        "url": url,
        "resubmit": False,
        "nsfw": False,
        "spoiler": False,
        "api_type": "json",
    }
    with httpx.Client(timeout=_TIMEOUT, headers=_api_headers(access_token)) as client:
        resp = client.post(f"{_API_BASE}/api/submit", data=payload)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"submit failed: HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    errors = data.get("json", {}).get("errors") or []
    if errors:
        raise RuntimeError(f"submit error: {errors[0]}")
    urls = data.get("json", {}).get("data", {})
    return {
        "url": urls.get("url", ""),
        "id": urls.get("id", ""),
        "permalink": urls.get("url", ""),
    }
