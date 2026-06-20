"""Find a podcast-host audio feed referenced from a website/episode page.

Many sites publish a Jekyll/WordPress *website* feed (rich show notes, but no
audio) while the actual MP3s live in a separate feed on a podcast host
(Libsyn, Buzzsprout, Transistor, Megaphone, Simplecast, …). That host feed is
almost always referenced on the episode page — both as a player embed and as a
``feeds.<host>/…`` URL. This module extracts that canonical audio-feed URL so the
app can suggest it (and, later, borrow its audio).

Pure text scanning — fetching happens in the app layer.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

import feedparser

# Known podcast-host feed URL shapes. Ordered roughly by how unambiguous they
# are; the first match wins. Each must match a full, directly-subscribable feed
# URL (not a web player page).
_HOST_FEED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"https?://feeds\.libsyn\.com/\d+/rss\b", re.I),
    re.compile(r"https?://feeds\.buzzsprout\.com/\d+\.rss\b", re.I),
    re.compile(r"https?://feeds\.transistor\.fm/[A-Za-z0-9._-]+", re.I),
    re.compile(r"https?://feeds\.megaphone\.fm/[A-Za-z0-9._-]+", re.I),
    re.compile(r"https?://feeds\.simplecast\.com/[A-Za-z0-9._-]+", re.I),
    re.compile(r"https?://feeds\.captivate\.fm/[A-Za-z0-9._-]+/?", re.I),
    re.compile(r"https?://feeds\.acast\.com/public/shows/[A-Za-z0-9._-]+", re.I),
    re.compile(r"https?://feeds\.redcircle\.com/[A-Za-z0-9._-]+", re.I),
    re.compile(r"https?://feeds\.blubrry\.com/feeds/[A-Za-z0-9._-]+\.xml", re.I),
    re.compile(r"https?://[A-Za-z0-9._-]+\.fireside\.fm/rss\b", re.I),
    re.compile(r"https?://feeds\.fireside\.fm/[A-Za-z0-9._-]+/rss\b", re.I),
    re.compile(r"https?://anchor\.fm/s/[A-Za-z0-9]+/podcast/rss\b", re.I),
    re.compile(r"https?://pinecast\.com/feed/[A-Za-z0-9._-]+", re.I),
    re.compile(r"https?://feed\.podbean\.com/[A-Za-z0-9._-]+/feed\.xml", re.I),
    re.compile(r"https?://[A-Za-z0-9._-]+\.podbean\.com/feed/?", re.I),
)

# Trailing characters a regex might greedily pull in from surrounding markup.
_TRAILING = "\"'<>)]}.,;& \t\r\n"


def find_podcast_host_feed(html: str | None) -> str | None:
    """Return the podcast-host feed URL referenced earliest in ``html``, or None.

    A non-None result is itself strong evidence the page is a podcast: only
    recognised host feeds match. When several are present, the one appearing
    first in the document wins (typically the page's own show, ahead of any
    "related shows" links).
    """
    if not html:
        return None
    best: tuple[int, str] | None = None
    for pat in _HOST_FEED_PATTERNS:
        m = pat.search(html)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), m.group(0).rstrip(_TRAILING))
    return best[1] if best else None


# --- episode audio matching (Stage 2: borrow audio into the website feed) ----

_AUDIO_EXTS = (".mp3", ".m4a", ".m4b", ".aac", ".ogg", ".oga", ".opus", ".flac", ".wav")
_EPNUM_RE = re.compile(r"\b(?:ep(?:isode)?\.?\s*)?(\d{1,4})\b", re.IGNORECASE)
_NORM_RE = re.compile(r"[^a-z0-9]+")


def _normalize_title(title: str) -> str:
    return _NORM_RE.sub(" ", (title or "").lower()).strip()


def _episode_number(title: str) -> str | None:
    m = _EPNUM_RE.search(title or "")
    return m.group(1).lstrip("0") or "0" if m else None


def _enclosure_audio_url(entry) -> str:
    for enc in entry.get("enclosures") or []:
        url = (enc.get("href") or enc.get("url") or "").strip()
        etype = (enc.get("type") or "").lower()
        if url and (etype.startswith("audio/")
                    or urlparse(url).path.lower().endswith(_AUDIO_EXTS)):
            return url
    return ""


def match_episode_audio(host_feed: bytes | str, titles: dict[str, str]) -> dict[str, str]:
    """Match website-feed entries to audio in a podcast-host feed.

    ``titles`` maps the website entry_id -> its title. Returns entry_id -> audio
    URL for every entry matched (by normalized title first, then episode number)
    to an audio enclosure in ``host_feed``. Unmatched entries are omitted.
    """
    parsed = feedparser.parse(host_feed)
    by_title: dict[str, str] = {}
    by_epnum: dict[str, str] = {}
    for e in parsed.entries:
        url = _enclosure_audio_url(e)
        if not url:
            continue
        title = e.get("title") or ""
        by_title.setdefault(_normalize_title(title), url)
        epnum = _episode_number(title)
        if epnum is not None:
            by_epnum.setdefault(epnum, url)

    out: dict[str, str] = {}
    for entry_id, title in titles.items():
        norm = _normalize_title(title)
        url = by_title.get(norm)
        if not url:
            epnum = _episode_number(title)
            if epnum is not None:
                url = by_epnum.get(epnum)
        if url:
            out[entry_id] = url
    return out
