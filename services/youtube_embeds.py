"""Recover YouTube embeds that feedparser/``reader`` strip from feed content.

feedparser sanitizes entry HTML and removes ``<iframe>`` (and other embed)
elements, so a WordPress post whose body is a YouTube block reaches the app as
an empty ``<figure class="wp-block-embed ... is-provider-youtube">`` shell — the
video id is gone. This module re-parses the raw feed with sanitization disabled
(where the original iframe survives) and extracts the YouTube video id(s) per
entry, so the app can rebuild a player.

Pure parsing only — fetching and persistence live in the app layer.
"""
from __future__ import annotations

import re

import feedparser

# A YouTube embed survives in feed content in a few shapes: the iframe src, a
# bare watch/share URL (oEmbed), or a youtu.be short link. Capture the 11-char
# video id from any of them.
_VIDEO_ID = r"([A-Za-z0-9_-]{11})"
_YT_ID_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"youtube(?:-nocookie)?\.com/embed/" + _VIDEO_ID, re.I),
    re.compile(r"youtube\.com/watch\?(?:[^\"'<>\s]*&)?v=" + _VIDEO_ID, re.I),
    re.compile(r"youtu\.be/" + _VIDEO_ID, re.I),
    re.compile(r"youtube\.com/v/" + _VIDEO_ID, re.I),
    re.compile(r"youtube\.com/shorts/" + _VIDEO_ID, re.I),
)
# Signals that an entry *intended* a YouTube embed even if no id is recoverable
# (so the app can record a negative result and stop re-scanning).
_YT_MARKER = re.compile(r"is-provider-youtube|wp-block-embed-youtube|youtube\.com|youtu\.be", re.I)


def _entry_html(entry: dict) -> str:
    parts = [c.get("value", "") for c in (entry.get("content") or [])]
    parts.append(entry.get("summary", "") or "")
    return "\n".join(p for p in parts if p)


def _video_ids(blob: str) -> list[str]:
    """Ordered, de-duplicated YouTube video ids found in ``blob``."""
    seen: set[str] = set()
    ids: list[str] = []
    for m in re.finditer(
        r"(?:youtube(?:-nocookie)?\.com/(?:embed/|watch\?[^\"'<>\s]*v=|v/|shorts/)|youtu\.be/)"
        + _VIDEO_ID,
        blob,
        re.I,
    ):
        vid = m.group(1)
        if vid not in seen:
            seen.add(vid)
            ids.append(vid)
    return ids


def video_ids_in_text(*texts: str) -> list[str]:
    """Ordered, de-duplicated YouTube video ids found across the given text blobs
    (entry link, content HTML, summary). Public wrapper around the internal scanner
    used by the auto-add-to-playlist automation."""
    return _video_ids("\n".join(t for t in texts if t))


def extract_youtube_embeds(raw: bytes | str) -> dict[str, list[str]]:
    """Map entry id (guid, falling back to link) -> ordered YouTube video ids.

    Entries that signalled a YouTube embed but yielded no recoverable id map to
    an empty list (a negative result the caller can cache). Entries with no
    YouTube embed at all are omitted.
    """
    parsed = feedparser.parse(raw, sanitize_html=False)
    out: dict[str, list[str]] = {}
    for entry in parsed.entries:
        key = entry.get("id") or entry.get("link") or ""
        if not key:
            continue
        blob = _entry_html(entry)
        if not blob or not _YT_MARKER.search(blob):
            continue
        out[key] = _video_ids(blob)
    return out
