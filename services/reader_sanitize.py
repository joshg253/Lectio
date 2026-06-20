"""A reader feed parser that keeps embeds instead of letting feedparser shred them.

``reader``'s built-in parser calls ``feedparser.parse(..., sanitize_html=True)``,
and feedparser's sanitizer *destroys* iframes, SVG, MathML, audio/video and many
attributes — silently stripping embeds from every article. This module mounts a
drop-in replacement that parses with sanitization **off** and then runs entry
content/summary through Lectio's own allowlist sanitizer (services.html_sanitize),
so safe embeds survive while scripts/handlers are still removed.

``reader`` does no sanitizing of its own, so once this is installed the stored
content is exactly what html_sanitize produced — safe to render with ``| safe``.
"""
from __future__ import annotations

import dataclasses
import logging

import feedparser

from reader._parser.feedparser import FeedparserParser, _process_feed

from services import html_sanitize

LOGGER = logging.getLogger("lectio.reader_sanitize")


def _sanitize_entry(entry):
    """Return ``entry`` with its content/summary run through html_sanitize."""
    changed = {}
    summary = getattr(entry, "summary", None)
    if isinstance(summary, str) and summary:
        changed["summary"] = html_sanitize.sanitize_html(summary)
    content = getattr(entry, "content", None)
    if content:
        new_content = tuple(
            dataclasses.replace(c, value=html_sanitize.sanitize_html(c.value))
            if isinstance(getattr(c, "value", None), str) and c.value
            else c
            for c in content
        )
        changed["content"] = new_content
    return dataclasses.replace(entry, **changed) if changed else entry


class SanitizingFeedparserParser(FeedparserParser):
    """Like reader's FeedparserParser, but sanitization is ours, not feedparser's."""

    def __call__(self, url, resource, headers=None):
        result = feedparser.parse(
            resource,
            resolve_relative_uris=True,
            sanitize_html=False,  # Lectio sanitizes instead (keeps safe embeds)
            response_headers=headers or {},
        )
        feed, entries = _process_feed(url, result)
        return feed, [_sanitize_entry(e) for e in entries]


def install(reader) -> None:
    """Replace reader's mounted FeedparserParser(s) with the sanitizing variant.

    Done via a lazy-init hook so it runs after reader mounts its own parsers.
    Replacing in place (rather than mounting a competitor) is order-independent:
    every MIME type the default handled now routes through ours.
    """
    replacement = SanitizingFeedparserParser()

    def _swap(parser) -> None:
        try:
            for _mime, plist in parser.parsers_by_mime_type.items():
                for i, (quality, p) in enumerate(plist):
                    if isinstance(p, FeedparserParser) and not isinstance(
                        p, SanitizingFeedparserParser
                    ):
                        plist[i] = (quality, replacement)
        except Exception:  # never let a parsing-plugin swap break reader init
            LOGGER.warning("could not install sanitizing feed parser", exc_info=True)

    reader._parser.lazy_init_funcs.insert(0, _swap)
