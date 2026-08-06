"""Publish-date sources beyond a page's own metadata.

`main.mine_publish_date` reads the metadata a well-behaved publisher ships —
``article:published_time``, JSON-LD ``datePublished``, ``<time datetime=…>``.
Plenty of sites ship none of it, and two shapes came up often enough to be worth
handling here:

1. **The date is on the page, but only as text a human reads.**
   hanselman.com prints ``<span class="blogMetaDate">February 03, 2026</span>``
   and nothing machine-readable at all. Mining metadata correctly found nothing,
   which read as "re-fetch won't set a date" for a page visibly showing one.

2. **The date is not on the article page at all, but the site publishes it
   elsewhere.** what-if.xkcd.com articles carry no date in any form (checked
   across 50 stored captures), while the site's archive index lists every
   article with its date. A per-article fetch can never find that; an index
   fetch finds all of them at once.

Shape 2 is a **site adapter**, deliberately not a branch in the caller: adding a
site means registering a resolver here, and the caller keeps asking one question.

Ordering matters and is the caller's job — these are strictly *worse* sources
than a publisher's own metadata and must be consulted after it, never instead of
it. Everything returned is range-checked; nothing here is trusted just because it
parsed.
"""
from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

LOGGER = logging.getLogger("lectio")

# Same bounds mine_publish_date uses: 1900/2099 are template placeholders and a
# future date is a clock problem, not a publication.
_MIN_YEAR = 1990
_FUTURE_SLACK = timedelta(days=2)

_MONTHS = ("january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december")


def _in_range(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if dt.year <= _MIN_YEAR:
        return None
    if dt > datetime.now(timezone.utc) + _FUTURE_SLACK:
        return None
    return dt


# ── 1. a date printed for humans ──────────────────────────────────────────────

# Only inside an element whose class/id says it is about the post's date. A page
# is full of dates — comment timestamps, "related posts" rails, a copyright
# footer — so matching bare date-shaped text anywhere would reliably pick the
# wrong one. This wants the element the publisher labelled.
_DATED_ELEMENT_RE = re.compile(
    r"<(?P<tag>span|div|p|time|small|li|h\d)\b[^>]*"
    r'(?:class|id)\s*=\s*["\'][^"\']*'
    r"(?:date|publish|posted|pubdate|timestamp|byline|meta)"
    r'[^"\']*["\'][^>]*>(?P<text>[^<]{4,60})<',
    re.I,
)

_TEXT_DATE_PATTERNS: tuple[tuple[str, str], ...] = (
    # "February 03, 2026" / "February 3 2026"
    (r"(" + "|".join(_MONTHS) + r")\s+(\d{1,2}),?\s+(\d{4})", "mdy"),
    # "3 February 2026"
    (r"(\d{1,2})\s+(" + "|".join(_MONTHS) + r"),?\s+(\d{4})", "dmy"),
    # "2026-02-03"
    (r"(\d{4})-(\d{2})-(\d{2})", "ymd"),
)


def _parse_text_date(text: str) -> datetime | None:
    low = text.strip().lower()
    for pattern, order in _TEXT_DATE_PATTERNS:
        m = re.search(pattern, low, re.I)
        if not m:
            continue
        try:
            if order == "mdy":
                year, month, day = int(m.group(3)), _MONTHS.index(m.group(1)) + 1, int(m.group(2))
            elif order == "dmy":
                year, month, day = int(m.group(3)), _MONTHS.index(m.group(2)) + 1, int(m.group(1))
            else:
                year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return datetime(year, month, day, tzinfo=timezone.utc)
        except (ValueError, IndexError):
            continue
    return None


def from_visible_text(raw_html: str | None) -> datetime | None:
    """A date the page shows a reader but never marks up.

    First labelled match wins: publishers put the post's own date above the fold
    and the noise (comments, related posts) below it.
    """
    if not raw_html:
        return None
    for m in _DATED_ELEMENT_RE.finditer(raw_html):
        found = _in_range(_parse_text_date(m.group("text")))
        if found is not None:
            return found
    return None


# ── 2. a date the site publishes somewhere other than the article ─────────────

# host -> resolver(entry_url) -> datetime | None
_SITE_RESOLVERS: dict[str, Callable[[str], datetime | None]] = {}


def register_site_resolver(host: str, resolver: Callable[[str], datetime | None]) -> None:
    _SITE_RESOLVERS[host.lower()] = resolver


def from_site_index(entry_url: str | None) -> datetime | None:
    """Ask the site's own index, for sites that publish dates only there."""
    if not entry_url:
        return None
    host = urlparse(entry_url).netloc.split("@")[-1].split(":")[0].lower()
    resolver = _SITE_RESOLVERS.get(host) or _SITE_RESOLVERS.get(host[4:] if host.startswith("www.") else "")
    if resolver is None:
        return None
    try:
        return _in_range(resolver(entry_url))
    except Exception:  # noqa: BLE001 — a date is a bonus, never a failure
        LOGGER.info("site date resolver failed for %s", entry_url, exc_info=True)
        return None


# ── what-if.xkcd.com: the archive index carries every article's date ──────────

WHATIF_ARCHIVE_URL = "https://what-if.xkcd.com/archive/"
_WHATIF_HOSTS = ("what-if.xkcd.com", "www.what-if.xkcd.com")

_WHATIF_ROW_RE = re.compile(
    r'<h2[^>]*class="archive-title"[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>.*?</h2>\s*'
    r'<h3[^>]*class="archive-date"[^>]*>\s*([^<]+?)\s*</h3>',
    re.I | re.S,
)
# http/https, www or not, trailing slash or not — the number is the identity.
_WHATIF_NUM_RE = re.compile(r"//(?:www\.)?what-if\.xkcd\.com/(\d+)", re.I)

_whatif_lock = threading.Lock()
_whatif_index: dict[str, datetime] | None = None


def whatif_article_number(url: str | None) -> str | None:
    m = _WHATIF_NUM_RE.search(url or "")
    return m.group(1) if m else None


def fetch_whatif_index(*, force: bool = False) -> dict[str, datetime]:
    """``{article_number: published}`` from the archive index.

    Fetched once per process and held: 162 articles arrive in one response, so
    dating a whole backlog costs a single request rather than one per article.
    """
    global _whatif_index
    with _whatif_lock:
        if _whatif_index is not None and not force:
            return _whatif_index
    from services import url_guard  # local: avoids an import cycle via main

    index: dict[str, datetime] = {}
    try:
        with url_guard.build_client(
            timeout=20.0, follow_redirects=True,
            headers={"User-Agent": "Lectio/0.1 (+https://github.com/joshg253/Lectio)"},
        ) as client:
            resp = url_guard.safe_get(client, WHATIF_ARCHIVE_URL)
        resp.raise_for_status()
        for href, date_text in _WHATIF_ROW_RE.findall(resp.text):
            number = whatif_article_number(href)
            if not number:
                continue
            try:
                index[number] = datetime.strptime(
                    date_text.strip(), "%B %d, %Y").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    except Exception:  # noqa: BLE001
        LOGGER.info("what-if archive index fetch failed", exc_info=True)
        return {}

    with _whatif_lock:
        # An empty parse means the page's markup changed; don't cache that as
        # truth, so the next call gets another chance.
        if index:
            _whatif_index = index
    return index


def _whatif_resolver(entry_url: str) -> datetime | None:
    number = whatif_article_number(entry_url)
    if not number:
        return None
    return fetch_whatif_index().get(number)


for _host in _WHATIF_HOSTS:
    register_site_resolver(_host, _whatif_resolver)


# ── the combined fallback the callers actually want ───────────────────────────


def resolve(raw_html: str | None, entry_url: str | None) -> tuple[datetime | None, str]:
    """Best non-metadata date for an entry, with the source that supplied it.

    Returns ``(None, "")`` when nothing does. Callers must try the page's own
    metadata first — these are the fallbacks for when that came up empty.
    """
    found = from_visible_text(raw_html)
    if found is not None:
        return found, "visible-text"
    found = from_site_index(entry_url)
    if found is not None:
        return found, "site-index"
    return None, ""
