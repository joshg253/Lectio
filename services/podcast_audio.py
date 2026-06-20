"""Recover podcast audio that lives only in ``<media:content>`` (Media RSS).

The ``reader`` library keeps standard ``<enclosure>`` elements but drops
``media:content`` / ``media:group``, so feeds that ship their audio only there
reach the app with no enclosure and no playable URL. This module re-parses the
raw feed with feedparser (which *does* surface ``media:content``) and extracts a
per-entry audio URL, so the app can fill in a player as a fallback.

Pure parsing only — fetching and persistence live in the app layer.
"""
from __future__ import annotations

from urllib.parse import urlparse

import feedparser

# Audio file extensions, matched on the URL *path* so signed/tokened query
# strings (common on podcast CDNs) don't defeat the check. Mirrors the app's
# enclosure detection.
_AUDIO_EXTS = (
    ".mp3", ".m4a", ".m4b", ".aac", ".ogg", ".oga", ".opus",
    ".flac", ".wav", ".mp4", ".m4v",
)


def _url_has_audio_ext(url: str) -> bool:
    if not url:
        return False
    return urlparse(url.strip()).path.lower().endswith(_AUDIO_EXTS)


def _media_item_is_audio(item: dict) -> bool:
    """True when a feedparser media_content item looks like audio."""
    medium = (item.get("medium") or "").lower()
    if medium == "audio":
        return True
    if medium in ("image", "video"):
        # Explicitly non-audio medium — don't fall back to the extension guess.
        return False
    mtype = (item.get("type") or "").lower()
    if mtype.startswith("audio/"):
        return True
    if mtype:
        # A non-audio MIME type (e.g. image/jpeg, video/mp4) — reject.
        return mtype in ("application/octet-stream",) and _url_has_audio_ext(item.get("url") or "")
    # No medium and no type — fall back to the URL extension.
    return _url_has_audio_ext(item.get("url") or "")


def extract_media_audio(raw: bytes | str) -> dict[str, str]:
    """Map entry id (guid, falling back to link) -> media:content audio URL.

    Only entries whose ``media:content`` (or ``media:group`` content, which
    feedparser flattens into ``media_content``) yields an audio item are
    included. Entries with no media audio are omitted.
    """
    parsed = feedparser.parse(raw)
    out: dict[str, str] = {}
    for entry in parsed.entries:
        items = entry.get("media_content") or []
        audio_url = ""
        for item in items:
            url = (item.get("url") or "").strip()
            if url and _media_item_is_audio(item):
                audio_url = url
                break
        if not audio_url:
            continue
        key = entry.get("id") or entry.get("link") or ""
        if key:
            out[key] = audio_url
    return out
