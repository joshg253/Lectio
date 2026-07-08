"""Dev.to filtered feeds as synthetic feeds.

Dev.to's RSS feeds (front page and per-tag) are unfiltered firehoses that mix
languages. Its public, unauthenticated JSON API exposes what the RSS doesn't:
a per-article ``language`` label, reaction counts, and a ``top=N`` ranking
window. We fetch ``GET https://dev.to/api/articles`` once per refresh, filter
client-side (the API ignores ``?language=``), and render the survivors to a
``file://`` RSS file the ``reader`` library subscribes to — the same pattern
as the DeviantArt and FakeFeedz adapters.

Per-feed config lives in the per-user meta DB (``devto_feeds``):
  tag           optional; empty = front page
  top_days      optional; N = dev.to's "top of last N days" ranking, empty = latest
  english_only  filter on the API's own ``language == "en"`` label (source's
                classification, deliberately not our own detection)
  min_reactions optional floor on positive_reactions_count
  tags_exclude  optional comma list passed straight to the API
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
from urllib.parse import urlparse

import httpx

from services import assert_safe_feed_id

LOGGER = logging.getLogger(__name__)

_API_URL = "https://dev.to/api/articles"
_USER_AGENT = "Lectio/1.0 (+https://github.com/joshg253/Lectio)"
_PER_PAGE = 80
_MAX_ENTRIES_PER_FEED = 100

_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 4.0  # seconds; doubles each retry on HTTP 429

_feeds_dir: Path | None = None

# Optional sink (feed_url, entry_id, image_url) -> None, set by main.py, used to
# push cover-image URLs into the lead-image service so dev.to posts get
# thumbnails without source-page scraping.
_lead_image_sink = None


def set_lead_image_sink(fn) -> None:
    global _lead_image_sink
    _lead_image_sink = fn


class DevToRateLimited(RuntimeError):
    """Raised when dev.to keeps returning HTTP 429 after retries."""


def init(data_dir: Path) -> None:
    global _feeds_dir
    _feeds_dir = data_dir / "devto-feeds"
    _feeds_dir.mkdir(parents=True, exist_ok=True)


def _dir() -> Path:
    assert _feeds_dir is not None, "devto.init() not called"
    return _feeds_dir


def feed_file_url(feed_id: str) -> str:
    return f"file://{_dir() / (feed_id + '.xml')}"


def devto_feed_id_from_url(file_url: str) -> str | None:
    """Extract our feed UUID from a file:// URL, or None if it's not ours.

    Dir-aware so it never matches a DeviantArt or FakeFeedz file URL.
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

def parse_devto_url(url: str) -> dict | None:
    """Recognize dev.to front-page/tag/feed URLs; return {"tag": str|None} or None.

    Handles:
      - https://dev.to/  (front page)
      - https://dev.to/feed
      - https://dev.to/t/python  (tag page, also /t/python/top/week etc.)
      - https://dev.to/feed/tag/python
    User/organization pages (dev.to/username) are NOT claimed — those RSS feeds
    are small and fine as-is.
    """
    try:
        u = urlparse(url.strip())
    except Exception:
        return None
    host = (u.netloc or "").lower()
    if host not in ("dev.to", "www.dev.to"):
        return None
    parts = [p for p in u.path.split("/") if p]
    if not parts:
        return {"tag": None}
    if parts[0] == "t" and len(parts) >= 2:
        return {"tag": parts[1].lower()}
    if parts[0] == "feed":
        if len(parts) == 1:
            return {"tag": None}
        if len(parts) >= 3 and parts[1] == "tag":
            return {"tag": parts[2].lower()}
    return None


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def _request(url: str, *, params: dict, timeout: float = 20.0):
    """GET with short backoff on 429; raises DevToRateLimited if it persists."""
    delay = _RETRY_BASE_DELAY
    last_resp = None
    with httpx.Client(timeout=timeout, headers={"User-Agent": _USER_AGENT}) as client:
        for attempt in range(_MAX_RETRIES):
            resp = client.get(url, params=params)
            if resp.status_code != 429:
                return resp
            last_resp = resp
            if attempt < _MAX_RETRIES - 1:
                try:
                    delay = float(resp.headers.get("Retry-After") or delay)
                except ValueError:
                    pass
                LOGGER.info("[devto] 429 rate-limited; backing off %.0fs", delay)
                time.sleep(delay)
                delay *= 2
    retry_after = last_resp.headers.get("Retry-After") if last_resp is not None else None
    msg = "dev.to request limit reached"
    if retry_after:
        msg += f" (retry after {retry_after}s)"
    raise DevToRateLimited(msg)


def _build_params(config: dict) -> dict:
    params: dict = {"per_page": _PER_PAGE}
    tag = (config.get("tag") or "").strip().lower()
    if tag:
        params["tag"] = tag
    top_days = config.get("top_days")
    if top_days:
        params["top"] = int(top_days)
    tags_exclude = (config.get("tags_exclude") or "").strip()
    if tags_exclude:
        params["tags_exclude"] = ",".join(
            t.strip().lower() for t in tags_exclude.split(",") if t.strip()
        )
    return params


def _passes_filters(article: dict, config: dict) -> bool:
    if config.get("english_only") and (article.get("language") or "") != "en":
        return False
    min_reactions = config.get("min_reactions")
    if min_reactions and int(article.get("positive_reactions_count") or 0) < int(min_reactions):
        return False
    return True


def fetch_articles(config: dict) -> list[dict]:
    """One polite API call, then client-side language/reactions filtering."""
    resp = _request(_API_URL, params=_build_params(config))
    if resp.status_code != 200:
        raise RuntimeError(f"dev.to fetch failed: HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        articles = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"dev.to fetch failed: invalid JSON ({exc})") from exc
    if not isinstance(articles, list):
        raise RuntimeError("dev.to fetch failed: unexpected response shape")
    return [a for a in articles if isinstance(a, dict) and _passes_filters(a, config)]


def _article_to_entry(a: dict) -> dict | None:
    """Normalize a dev.to article object to our entry shape, or None to skip."""
    article_id = a.get("id")
    link = a.get("url")
    if not article_id or not link:
        return None
    title = a.get("title") or link
    img = a.get("cover_image") or ""
    ts = a.get("published_timestamp") or a.get("published_at")
    try:
        published_at = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).isoformat()
    except Exception:
        published_at = datetime.now(timezone.utc).isoformat()
    author = (a.get("user") or {}).get("name") or ""
    parts = []
    if img:
        parts.append(f'<p><a href="{_esc(link)}"><img src="{_esc(img)}" alt="{_esc(str(title))}"></a></p>')
    desc = a.get("description") or ""
    if desc:
        parts.append(f"<p>{_esc(desc)}</p>")
    meta_bits = []
    if author:
        meta_bits.append(f"by {_esc(author)}")
    reactions = a.get("positive_reactions_count")
    if reactions:
        meta_bits.append(f"{int(reactions)} reactions")
    minutes = a.get("reading_time_minutes")
    if minutes:
        meta_bits.append(f"{int(minutes)} min read")
    tags = a.get("tag_list") or []
    if isinstance(tags, list) and tags:
        meta_bits.append(_esc(", ".join(f"#{t}" for t in tags)))
    if meta_bits:
        parts.append(f'<p>{" · ".join(meta_bits)}</p>')
    return {
        "id": str(article_id),
        "title": str(title),
        "entry_url": link,
        "content": "".join(parts),
        "published_at": published_at,
        "image_src": img,
        "tags": [str(t) for t in tags] if isinstance(tags, list) else [],
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
        # <category> per tag: ingest captures these into entry_feed_tags
        # (suggestion chips) via the sanitizing parser's tag sink.
        + "".join(
            f"      <category>{_esc(str(t))}</category>\n"
            for t in (e.get("tags") or [])
        )
        + "    </item>"
    )


def _generate_rss_xml(feed_title: str, source_url: str, entries: list[dict]) -> str:
    items = "\n".join(_item_xml(e) for e in entries)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0">\n'
        "  <channel>\n"
        f"    <title><![CDATA[{feed_title}]]></title>\n"
        f"    <link>{_esc(source_url)}</link>\n"
        "    <description>dev.to filtered feed — generated by Lectio</description>\n"
        f"{items}\n"
        "  </channel>\n"
        "</rss>\n"
    )


def _page_url(tag: str | None) -> str:
    return f"https://dev.to/t/{tag}" if tag else "https://dev.to/"


def default_title(config: dict) -> str:
    tag = (config.get("tag") or "").strip()
    base = f"dev.to #{tag}" if tag else "dev.to"
    bits = []
    if config.get("top_days"):
        bits.append(f"top {int(config['top_days'])}d")
    if config.get("min_reactions"):
        bits.append(f"≥{int(config['min_reactions'])} reactions")
    return f"{base} ({', '.join(bits)})" if bits else base


def _write_feed_file(conn: sqlite3.Connection, feed_id: str) -> None:
    assert_safe_feed_id(feed_id)
    row = conn.execute("SELECT * FROM devto_feeds WHERE id = ?", (feed_id,)).fetchone()
    if not row:
        return
    rows = conn.execute(
        "SELECT * FROM devto_entries WHERE devto_feed_id = ?"
        " ORDER BY published_at DESC LIMIT ?",
        (feed_id, _MAX_ENTRIES_PER_FEED),
    ).fetchall()
    entries = [
        {"id": r["article_id"], "title": r["title"], "entry_url": r["entry_url"],
         "content": r["content"], "published_at": r["published_at"]}
        for r in rows
    ]
    xml = _generate_rss_xml(str(row["feed_title"]), _page_url(row["tag"]), entries)
    (_dir() / f"{feed_id}.xml").write_text(xml, encoding="utf-8")


def _upsert_entries(conn: sqlite3.Connection, feed_id: str, articles: list[dict]) -> int:
    """Insert new articles; returns count of newly-added entries.

    Seeds the lead-image cache with the API's cover_image so dev.to posts get
    thumbnails deterministically (no source-page scrape). Fills empty rows only.
    """
    file_url = feed_file_url(feed_id)
    added = 0
    for a in articles:
        e = _article_to_entry(a)
        if not e:
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO devto_entries"
            " (id, devto_feed_id, article_id, title, entry_url, content, published_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), feed_id, e["id"], e["title"], e["entry_url"], e["content"], e["published_at"]),
        )
        added += cur.rowcount
        # entry_id in the lead-image service is the reader entry id = our <guid>.
        if e["image_src"] and _lead_image_sink is not None:
            try:
                _lead_image_sink(file_url, e["id"], e["image_src"])
            except Exception:
                LOGGER.exception("[devto] lead-image seed failed for %s", e["id"])
    return added


# ---------------------------------------------------------------------------
# Feed lifecycle
# ---------------------------------------------------------------------------

def _config_from_row(row) -> dict:
    return {
        "tag": row["tag"],
        "top_days": row["top_days"],
        "english_only": bool(row["english_only"]),
        "min_reactions": row["min_reactions"],
        "tags_exclude": row["tags_exclude"],
    }


def get_feed_config(conn: sqlite3.Connection, feed_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM devto_feeds WHERE id = ?", (feed_id,)).fetchone()
    if not row:
        return None
    cfg = _config_from_row(row)
    cfg["feed_title"] = row["feed_title"]
    return cfg


def create_devto_feed(conn: sqlite3.Connection, reader, config: dict,
                      feed_title: str | None = None) -> tuple[str, str]:
    """Create a dev.to filtered feed and register it with reader.

    Caller adds it to a folder. Raises on API errors so a typo'd tag fails
    loudly instead of subscribing to an empty feed.
    """
    articles = fetch_articles(config)
    title = feed_title or default_title(config)
    feed_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO devto_feeds (id, feed_title, tag, top_days, english_only,"
        " min_reactions, tags_exclude, created_at, last_synced_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (feed_id, title, (config.get("tag") or "").strip().lower() or None,
         config.get("top_days"), 1 if config.get("english_only") else 0,
         config.get("min_reactions"), (config.get("tags_exclude") or "").strip() or None,
         now, now),
    )
    _upsert_entries(conn, feed_id, articles)
    _write_feed_file(conn, feed_id)

    file_url = feed_file_url(feed_id)
    reader.add_feed(file_url, exist_ok=True)
    try:
        reader.update_feed(file_url)
    except Exception:
        pass
    return feed_id, file_url


def update_devto_feed_config(conn: sqlite3.Connection, reader, feed_id: str, config: dict) -> None:
    """Update a feed's filter config, re-fetch, and rewrite its file.

    Already-ingested entries that the new filters would exclude stay in reader
    (history is kept); the filters shape what arrives from now on.
    """
    assert_safe_feed_id(feed_id)
    row = conn.execute("SELECT * FROM devto_feeds WHERE id = ?", (feed_id,)).fetchone()
    if not row:
        raise ValueError(f"unknown devto feed {feed_id}")
    # Regenerate the auto title so it reflects the new filters (e.g. the
    # "top 7d, ≥10 reactions" suffix). Only when the stored title is still the
    # auto-generated one for the OLD config — a hand-picked title is kept.
    # (Display-name overrides live in reader's user_title and are unaffected.)
    feed_title = str(row["feed_title"])
    if feed_title == default_title(_config_from_row(row)):
        feed_title = default_title(config)
    conn.execute(
        "UPDATE devto_feeds SET feed_title = ?, tag = ?, top_days = ?, english_only = ?,"
        " min_reactions = ?, tags_exclude = ? WHERE id = ?",
        (feed_title, (config.get("tag") or "").strip().lower() or None, config.get("top_days"),
         1 if config.get("english_only") else 0, config.get("min_reactions"),
         (config.get("tags_exclude") or "").strip() or None, feed_id),
    )
    refresh_devto_feed_by_id(conn, feed_id)
    try:
        reader.update_feed(feed_file_url(feed_id))
    except Exception:
        pass


def refresh_devto_feed_by_id(conn: sqlite3.Connection, feed_id: str) -> int:
    """Re-fetch a feed's articles and rewrite its file. Returns new-entry count."""
    row = conn.execute("SELECT * FROM devto_feeds WHERE id = ?", (feed_id,)).fetchone()
    if not row:
        return 0
    articles = fetch_articles(_config_from_row(row))
    added = _upsert_entries(conn, feed_id, articles)
    conn.execute(
        "UPDATE devto_feeds SET last_synced_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), feed_id),
    )
    _write_feed_file(conn, feed_id)
    return added


def refresh_all_devto_feeds(conn: sqlite3.Connection, max_feeds: int = 40) -> None:
    """Refresh dev.to feeds, oldest-synced first, capped at `max_feeds` per call.

    Stops early and quietly if dev.to rate-limits us. `max_feeds<=0` = no cap.
    """
    try:
        query = "SELECT id FROM devto_feeds ORDER BY last_synced_at ASC"
        if max_feeds and max_feeds > 0:
            query += f" LIMIT {int(max_feeds)}"
        rows = conn.execute(query).fetchall()
    except sqlite3.OperationalError as exc:
        # Table may legitimately not exist in some test envs; anything else
        # (renamed column, bad migration) still surfaces at debug level.
        LOGGER.debug("[devto] skipping refresh; devto_feeds unavailable: %s", exc)
        return
    for row in rows:
        try:
            refresh_devto_feed_by_id(conn, str(row["id"]))
        except DevToRateLimited:
            LOGGER.info("[devto] refresh hit rate limit; stopping this cycle")
            return
        except Exception:
            LOGGER.exception("[devto] error refreshing feed %s", row["id"])


def delete_devto_feed(conn: sqlite3.Connection, reader, feed_id: str) -> None:
    assert_safe_feed_id(feed_id)
    file_url = feed_file_url(feed_id)
    conn.execute("DELETE FROM devto_entries WHERE devto_feed_id = ?", (feed_id,))
    conn.execute("DELETE FROM devto_feeds WHERE id = ?", (feed_id,))
    try:
        (_dir() / f"{feed_id}.xml").unlink(missing_ok=True)
    except Exception:
        pass
    try:
        reader.delete_feed(file_url, missing_ok=True)
    except Exception:
        pass
