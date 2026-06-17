"""DeviantArt galleries as synthetic feeds.

DeviantArt locked its legacy ``backend.deviantart.com/rss.xml`` endpoint behind a
CloudFront WAF that 403s datacenter traffic. Instead we fetch public galleries
through the official DeviantArt API (OAuth2 *client-credentials* grant — app key
only, no user login) and render them to ``file://`` RSS files, exactly like the
FakeFeedz scraper. The ``reader`` library subscribes to the file URL, so
read/unread, starring, tags, and automation all work out of the box.

Credentials are **per-user** (client_id + client_secret). Callers pass them in;
main.py reads them from each user's app-settings.

Phase 2 (watch-list sync via the authorization_code grant) builds on this.
"""
from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from email.utils import format_datetime as _format_rfc2822
from html import escape as _esc
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

LOGGER = logging.getLogger(__name__)

_TOKEN_URL = "https://www.deviantart.com/oauth2/token"
_AUTHORIZE_URL = "https://www.deviantart.com/oauth2/authorize"
_API_BASE = "https://www.deviantart.com/api/v1/oauth2"
_USER_AGENT = "Lectio/1.0 (+https://github.com/joshg253/Lectio)"
# browse: read galleries; user: read your watch list; user.manage: add watches.
_OAUTH_SCOPE = "browse user user.manage"
_MAX_ENTRIES_PER_FEED = 50
_PAGE_SIZE = 24

_feeds_dir: Path | None = None

# Optional sink (feed_url, entry_id, image_url) -> None, set by main.py, used to
# push API-provided image URLs into the lead-image service (DB + live cache) so DA
# posts get thumbnails deterministically without source-page scraping.
_lead_image_sink = None


def set_lead_image_sink(fn) -> None:
    global _lead_image_sink
    _lead_image_sink = fn

_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 4.0  # seconds; doubles each retry on HTTP 429


class DeviantArtRateLimited(RuntimeError):
    """Raised when DeviantArt's per-user request quota is exhausted (HTTP 429)."""


def _request(method: str, url: str, *, headers: dict, params: dict | None = None,
             data: dict | None = None, timeout: float = 20.0):
    """HTTP request with short backoff on 429; raises DeviantArtRateLimited if the
    quota stays exhausted (so bulk callers can stop fast instead of churning)."""
    delay = _RETRY_BASE_DELAY
    # One client for the whole call: retries reuse the connection pool instead of
    # paying TCP/TLS setup again on every 429 backoff.
    with httpx.Client(timeout=timeout, headers=headers) as client:
        for attempt in range(_MAX_RETRIES):
            resp = client.request(method, url, params=params, data=data)
            if resp.status_code != 429:
                return resp
            if attempt < _MAX_RETRIES - 1:
                LOGGER.info("[deviantart] 429 rate-limited; backing off %.0fs", delay)
                time.sleep(delay)
                delay *= 2
    raise DeviantArtRateLimited("DeviantArt per-user request limit reached")

# Cache of client_id -> (access_token, expires_at_epoch). Tokens are app-scoped,
# so one per client_id is correct even across users sharing creds.
_token_cache: dict[str, tuple[str, float]] = {}


def init(data_dir: Path) -> None:
    global _feeds_dir
    _feeds_dir = data_dir / "deviantart-feeds"
    _feeds_dir.mkdir(parents=True, exist_ok=True)


def _dir() -> Path:
    assert _feeds_dir is not None, "deviantart.init() not called"
    return _feeds_dir


def feed_file_url(feed_id: str) -> str:
    return f"file://{_dir() / (feed_id + '.xml')}"


def deviantart_feed_id_from_url(file_url: str) -> str | None:
    """Extract our feed UUID from a file:// URL, or None if it's not ours.

    Dir-aware so it never matches a FakeFeedz scraped-feed file URL.
    """
    if not file_url.startswith("file://"):
        return None
    p = Path(file_url[len("file://"):])
    if p.parent != _dir():
        return None
    return p.stem or None


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------

def username_from_url(url: str) -> str | None:
    """Pull a DeviantArt gallery username from any DA URL form, else None.

    Handles:
      - https://backend.deviantart.com/rss.xml?q=gallery:USER&type=deviation
      - https://www.deviantart.com/USER  (and /USER/gallery, /USER/gallery/all)
      - https://USER.deviantart.com/  (legacy subdomain form)
    """
    try:
        u = urlparse(url.strip())
    except Exception:
        return None
    host = (u.netloc or "").lower()
    if "deviantart.com" not in host:
        return None

    # Legacy RSS: ?q=gallery:USER
    if host == "backend.deviantart.com":
        q = parse_qs(u.query).get("q", [""])[0]
        if q.startswith("gallery:"):
            name = q[len("gallery:"):].split("/")[0].strip()
            return name or None
        return None

    # Legacy subdomain: USER.deviantart.com
    if host not in ("www.deviantart.com", "deviantart.com"):
        sub = host.split(".deviantart.com")[0]
        if sub and sub not in ("www", "backend", "fav", "comments"):
            return sub

    # Path form: /USER[/gallery...]
    parts = [p for p in u.path.split("/") if p]
    _reserved = {"users", "join", "settings", "notifications", "watch", "tag", "search"}
    if parts and parts[0].lower() not in _reserved:
        return parts[0]
    return None


def is_deviantart_url(url: str) -> bool:
    return username_from_url(url) is not None


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def _get_token(client_id: str, client_secret: str) -> str:
    cached = _token_cache.get(client_id)
    if cached and cached[1] - 60 > time.time():
        return cached[0]
    with httpx.Client(timeout=20, headers={"User-Agent": _USER_AGENT}) as client:
        resp = client.post(
            _TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
    if resp.status_code != 200:
        raise RuntimeError(f"token request failed: HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"token response missing access_token: {data}")
    _token_cache[client_id] = (token, time.time() + float(data.get("expires_in", 3600)))
    return token


def verify_credentials(client_id: str, client_secret: str) -> tuple[bool, str]:
    """Return (ok, message). Used by the Settings UI to validate before saving."""
    if not client_id or not client_secret:
        return False, "client_id and client_secret are required."
    try:
        _token_cache.pop(client_id, None)
        _get_token(client_id, client_secret)
        return True, "Credentials verified."
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "invalid_client" in msg or "public client" in msg.lower():
            return False, ("Saved. Key validation isn't available for public-client apps — "
                           "just click “Connect DeviantArt account” to authorize.")
        return False, msg


def fetch_gallery(client_id: str, client_secret: str, username: str,
                  limit: int = _MAX_ENTRIES_PER_FEED, access_token: str = "") -> list[dict]:
    """Fetch up to `limit` deviations from a user's 'all' gallery.

    Uses the provided user `access_token` when given (required for apps DeviantArt
    classifies as "public clients", which can't use the client_credentials grant);
    otherwise falls back to an app-only client-credentials token.
    """
    token = access_token or _get_token(client_id, client_secret)
    headers = {"User-Agent": _USER_AGENT, "Authorization": f"Bearer {token}"}
    out: list[dict] = []
    offset = 0
    while len(out) < limit:
        resp = _request("GET", f"{_API_BASE}/gallery/all", headers=headers, params={
            "username": username, "offset": offset, "limit": _PAGE_SIZE, "mature_content": "true",
        })
        if resp.status_code != 200:
            raise RuntimeError(f"gallery fetch failed for {username}: HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        results = data.get("results") or []
        out.extend(results)
        if not data.get("has_more") or not results:
            break
        offset = data.get("next_offset") or (offset + _PAGE_SIZE)
    return out[:limit]


def fetch_watch_feed(access_token: str, limit: int = _MAX_ENTRIES_PER_FEED) -> list[dict]:
    """Fetch recent deviations from everyone the user Watches (one combined stream).

    Uses /browse/deviantsyouwatch — a handful of paginated calls total, instead of
    one gallery fetch per watched artist. Requires the user access token.
    """
    headers = _user_headers(access_token)
    out: list[dict] = []
    offset = 0
    while len(out) < limit:
        resp = _request("GET", f"{_API_BASE}/browse/deviantsyouwatch", headers=headers,
                        params={"offset": offset, "limit": _PAGE_SIZE, "mature_content": "true"})
        if resp.status_code != 200:
            raise RuntimeError(f"watch feed fetch failed: HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        results = data.get("results") or []
        out.extend(results)
        if not data.get("has_more") or not results:
            break
        offset = data.get("next_offset") or (offset + _PAGE_SIZE)
    return out[:limit]


def _deviation_to_entry(d: dict) -> dict | None:
    """Normalize a DA deviation object to our entry shape, or None to skip."""
    devid = d.get("deviationid")
    link = d.get("url")
    if not devid or not link:
        return None
    title = d.get("title") or link
    # Prefer the full image; fall back to the largest available thumb.
    img = ((d.get("content") or {}).get("src")) or ""
    if not img:
        thumbs = d.get("thumbs") or []
        if thumbs:
            img = thumbs[-1].get("src", "")
    ts = d.get("published_time")
    try:
        published_at = datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except Exception:
        published_at = datetime.now(timezone.utc).isoformat()
    author = (d.get("author") or {}).get("username") or ""
    parts = []
    if img:
        parts.append(f'<p><a href="{_esc(link)}"><img src="{_esc(img)}" alt="{_esc(title)}"></a></p>')
    if author:
        parts.append(f'<p>by {_esc(author)} on DeviantArt</p>')
    return {
        "id": str(devid),
        "title": title,
        "entry_url": link,
        "content": "".join(parts),
        "published_at": published_at,
        "is_mature": bool(d.get("is_mature")),
        "image_src": img or "",
    }


# ---------------------------------------------------------------------------
# RSS file generation
# ---------------------------------------------------------------------------

def _item_xml(e: dict) -> str:
    try:
        dt = datetime.fromisoformat(str(e["published_at"]))
        pub = f"<pubDate>{_format_rfc2822(dt)}</pubDate>"
    except Exception:
        pub = ""
    return (
        "    <item>\n"
        f"      <title><![CDATA[{e['title']}]]></title>\n"
        f"      <link>{_esc(str(e['entry_url']))}</link>\n"
        f"      <guid isPermaLink=\"false\">{_esc(str(e['id']))}</guid>\n"
        f"      {pub}\n"
        f"      <description><![CDATA[{e.get('content') or ''}]]></description>\n"
        "    </item>"
    )


def _generate_rss_xml(feed_title: str, source_url: str, entries: list[dict]) -> str:
    items = "\n".join(_item_xml(e) for e in entries)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0">\n'
        "  <channel>\n"
        f"    <title><![CDATA[{feed_title}]]></title>\n"
        f"    <link>{_esc(source_url)}</link>\n"
        "    <description>DeviantArt gallery — generated by Lectio</description>\n"
        f"{items}\n"
        "  </channel>\n"
        "</rss>\n"
    )


def _gallery_page_url(username: str) -> str:
    return f"https://www.deviantart.com/{username}/gallery/all"


def _write_feed_file(conn: sqlite3.Connection, feed_id: str) -> None:
    row = conn.execute("SELECT * FROM deviantart_feeds WHERE id = ?", (feed_id,)).fetchone()
    if not row:
        return
    rows = conn.execute(
        "SELECT * FROM deviantart_entries WHERE deviantart_feed_id = ?"
        " ORDER BY published_at DESC LIMIT ?",
        (feed_id, _MAX_ENTRIES_PER_FEED),
    ).fetchall()
    entries = [
        {"id": r["deviationid"], "title": r["title"], "entry_url": r["entry_url"],
         "content": r["content"], "published_at": r["published_at"]}
        for r in rows
    ]
    xml = _generate_rss_xml(str(row["feed_title"]), _gallery_page_url(str(row["username"])), entries)
    (_dir() / f"{feed_id}.xml").write_text(xml, encoding="utf-8")


def _upsert_entries(conn: sqlite3.Connection, feed_id: str, deviations: list[dict]) -> int:
    """Insert new deviations; returns count of newly-added entries.

    Also seeds the lead-image cache with the API-provided image URL so DeviantArt
    posts get their thumbnail/lead image deterministically (no source-page scrape,
    which whiffs on some deviations). Only fills empty rows — never clobbers a
    populated/customized one.
    """
    file_url = feed_file_url(feed_id)
    added = 0
    for d in deviations:
        e = _deviation_to_entry(d)
        if not e:
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO deviantart_entries"
            " (id, deviantart_feed_id, deviationid, title, entry_url, content, published_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), feed_id, e["id"], e["title"], e["entry_url"], e["content"], e["published_at"]),
        )
        added += cur.rowcount
        # Seed the lead-image service (DB + live cache) with the API image URL.
        # entry_id there is the reader entry id, which equals our <guid> = deviationid.
        if e["image_src"] and _lead_image_sink is not None:
            try:
                _lead_image_sink(file_url, e["id"], e["image_src"])
            except Exception:
                # Seeding the thumbnail is best-effort; log so a persistent sink
                # failure is visible rather than silently leaving posts blank.
                LOGGER.exception("[deviantart] lead-image seed failed for %s", e["id"])
    return added


# ---------------------------------------------------------------------------
# Feed lifecycle
# ---------------------------------------------------------------------------

def create_deviantart_feed(
    conn: sqlite3.Connection,
    reader,
    username: str,
    client_id: str,
    client_secret: str,
    feed_title: str | None = None,
    access_token: str = "",
    limit: int = _MAX_ENTRIES_PER_FEED,
) -> tuple[str, str]:
    """Create a DeviantArt gallery feed and register it with reader.

    Caller adds it to a folder. Raises on API/credential errors. `limit` caps the
    initial fetch — bulk syncs pass a smaller value (one page) to conserve the
    DeviantArt API quota; scheduled refreshes later top the feed up.
    """
    username = username.strip().lstrip("@")
    if not username:
        raise ValueError("username required")

    deviations = fetch_gallery(client_id, client_secret, username, limit=limit, access_token=access_token)
    title = feed_title or f"{username} on DeviantArt"

    feed_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO deviantart_feeds (id, username, feed_title, created_at, last_synced_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (feed_id, username, title, now, now),
    )
    _upsert_entries(conn, feed_id, deviations)
    _write_feed_file(conn, feed_id)

    file_url = feed_file_url(feed_id)
    reader.add_feed(file_url, exist_ok=True)
    try:
        reader.update_feed(file_url)
    except Exception:
        pass
    return feed_id, file_url


def create_watch_feed(conn: sqlite3.Connection, reader, access_token: str,
                      feed_title: str = "DeviantArt — Watching") -> tuple[str, str]:
    """Create the single combined 'deviations from everyone you Watch' feed."""
    if not access_token:
        raise ValueError("DeviantArt account not connected")
    feed_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO deviantart_feeds (id, username, feed_title, created_at, last_synced_at, source)"
        " VALUES (?, ?, ?, ?, ?, 'watch')",
        (feed_id, "deviantsyouwatch", feed_title, now, now),
    )
    _upsert_entries(conn, feed_id, fetch_watch_feed(access_token))
    _write_feed_file(conn, feed_id)
    file_url = feed_file_url(feed_id)
    reader.add_feed(file_url, exist_ok=True)
    try:
        reader.update_feed(file_url)
    except Exception:
        pass
    return feed_id, file_url


def _feed_source(row) -> str:
    try:
        return str(row["source"] or "gallery")
    except (IndexError, KeyError):
        return "gallery"


def refresh_deviantart_feed_by_id(conn: sqlite3.Connection, feed_id: str, client_id: str,
                                  client_secret: str, access_token: str = "") -> int:
    """Re-fetch a gallery (or the watch stream) and rewrite its file. Returns new entries."""
    row = conn.execute("SELECT * FROM deviantart_feeds WHERE id = ?", (feed_id,)).fetchone()
    if not row:
        return 0
    if _feed_source(row) == "watch":
        if not access_token:
            return 0  # watch feed needs the user token
        deviations = fetch_watch_feed(access_token)
    else:
        deviations = fetch_gallery(client_id, client_secret, str(row["username"]), access_token=access_token)
    added = _upsert_entries(conn, feed_id, deviations)
    conn.execute(
        "UPDATE deviantart_feeds SET last_synced_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), feed_id),
    )
    _write_feed_file(conn, feed_id)
    return added


def refresh_all_deviantart_feeds(conn: sqlite3.Connection, client_id: str, client_secret: str,
                                 access_token: str = "", max_feeds: int = 40) -> None:
    """Refresh DeviantArt feeds, oldest-synced first, capped at `max_feeds` per call.

    The cap (round-robin via last_synced_at) keeps a large watch-list folder from
    blowing through DeviantArt's per-user rate limit on every scheduler tick.
    Stops early and quietly if the quota is hit. `max_feeds<=0` means no cap.
    """
    try:
        query = "SELECT id FROM deviantart_feeds ORDER BY last_synced_at ASC"
        if max_feeds and max_feeds > 0:
            query += f" LIMIT {int(max_feeds)}"
        rows = conn.execute(query).fetchall()
    except Exception:
        return  # table may not exist in some test envs
    if not rows:
        return
    if not access_token and not (client_id and client_secret):
        LOGGER.info("[deviantart] %d feed(s) but no credentials configured; skipping refresh", len(rows))
        return
    for row in rows:
        try:
            refresh_deviantart_feed_by_id(conn, str(row["id"]), client_id, client_secret, access_token=access_token)
        except DeviantArtRateLimited:
            LOGGER.info("[deviantart] refresh hit rate limit; stopping this cycle")
            return
        except Exception:
            LOGGER.exception("[deviantart] error refreshing feed %s", row["id"])


def delete_deviantart_feed(conn: sqlite3.Connection, reader, feed_id: str) -> None:
    file_url = feed_file_url(feed_id)
    conn.execute("DELETE FROM deviantart_entries WHERE deviantart_feed_id = ?", (feed_id,))
    conn.execute("DELETE FROM deviantart_feeds WHERE id = ?", (feed_id,))
    try:
        (_dir() / f"{feed_id}.xml").unlink(missing_ok=True)
    except Exception:
        pass
    try:
        reader.delete_feed(file_url, missing_ok=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# OAuth2 authorization_code flow (Phase 2: watch-list sync)
# ---------------------------------------------------------------------------

def generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for the PKCE S256 flow.

    DeviantArt requires PKCE on the authorization_code grant.
    """
    import base64
    import hashlib
    import secrets
    verifier = secrets.token_urlsafe(64)  # 86 chars, within the 43–128 range
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(client_id: str, redirect_uri: str, state: str, code_challenge: str) -> str:
    from urllib.parse import urlencode
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": _OAUTH_SCOPE,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{_AUTHORIZE_URL}?{urlencode(params)}"


def _post_token(payload: dict, client_secret: str, what: str) -> dict:
    """POST to the token endpoint, tolerating both confidential and public clients.

    Tries with the client_secret first (confidential); if the client can't
    authenticate that way (public client → invalid_client), retries without it,
    relying on PKCE. Returns the token dict or raises.
    """
    bodies = ([{**payload, "client_secret": client_secret}] if client_secret else []) + [payload]
    last = None
    for body in bodies:
        with httpx.Client(timeout=20, headers={"User-Agent": _USER_AGENT}) as client:
            resp = client.post(_TOKEN_URL, data=body)
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if resp.status_code == 200 and data.get("access_token"):
            return data
        last = resp
    raise RuntimeError(f"{what} failed: HTTP {last.status_code}: {last.text[:200]}")


def exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str, code_verifier: str) -> dict:
    """Exchange an authorization code for access + refresh tokens (PKCE)."""
    return _post_token({
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }, client_secret, "token exchange")


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> dict:
    """Refresh an expired access token. Returns the new token dict."""
    return _post_token({
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token,
    }, client_secret, "token refresh")


def _user_headers(access_token: str) -> dict:
    return {"User-Agent": _USER_AGENT, "Authorization": f"Bearer {access_token}"}


def whoami(access_token: str) -> str:
    """Return the authenticated user's DeviantArt username."""
    resp = _request("GET", f"{_API_BASE}/user/whoami", headers=_user_headers(access_token))
    if resp.status_code != 200:
        raise RuntimeError(f"whoami failed: HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json().get("username", "")


def list_watching(access_token: str, username: str) -> list[str]:
    """Return the list of usernames the given user Watches."""
    out: list[str] = []
    offset = 0
    headers = _user_headers(access_token)
    while True:
        resp = _request("GET", f"{_API_BASE}/user/friends/{username}", headers=headers,
                        params={"offset": offset, "limit": 50})
        if resp.status_code != 200:
            raise RuntimeError(f"friends list failed: HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        for item in data.get("results") or []:
            name = (item.get("user") or {}).get("username")
            if name:
                out.append(name)
        if not data.get("has_more"):
            break
        offset = data.get("next_offset") or (offset + 50)
    return out


def watch_user(access_token: str, username: str) -> tuple[bool, str]:
    """Add `username` to the authenticated user's Watch list. Returns (ok, message)."""
    body = {f"watch[{k}]": "1" for k in
            ("friend", "deviations", "journals", "forum_threads", "critiques", "scraps", "activity", "collections")}
    resp = _request("POST", f"{_API_BASE}/user/friends/watch/{username}", headers=_user_headers(access_token), data=body)
    if resp.status_code == 200 and (resp.json().get("success") if resp.headers.get("content-type", "").startswith("application/json") else False):
        return True, "ok"
    return False, f"HTTP {resp.status_code}: {resp.text[:160]}"
