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
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

# Use the EXACT feedparser module reader uses (it may be the vendored copy,
# reader._vendor.feedparser). reader's _process_feed decides which bozo
# exceptions are survivable via isinstance against that module's exception
# classes; importing the standalone `feedparser` here would yield different
# classes, so survivable bozos (e.g. NonXMLContentType) would wrongly raise
# ParseError and break every feed update that returns a body.
from reader._parser.feedparser import (
    FeedparserParser,
    _process_feed,
    feedparser,
)

from services import html_sanitize

LOGGER = logging.getLogger("lectio.reader_sanitize")

# Injected sink for feed-provided entry tags: fn(feed_url, [(entry_id, tags)]).
# reader discards <category> data at ingest, so the parser (the only place the
# raw feedparser result exists) hands tags to whoever registered here
# (FeedTagService in main). Parsing runs synchronously in the caller's tenancy
# context, so the sink's meta-DB writes land in the right per-user DB.
_entry_tag_sink = None


def set_entry_tag_sink(sink) -> None:
    global _entry_tag_sink
    _entry_tag_sink = sink


# Injected sink for the feed's current window: fn(feed_url, [entry_ids]).
# Records which entries the publisher is still serving, so tomb sweeping never
# drops a tombstone whose entry could be re-ingested (a quiet YouTube channel
# serves the same 15 videos for years).
_feed_window_sink = None


def set_feed_window_sink(sink) -> None:
    global _feed_window_sink
    _feed_window_sink = sink


def _record_feed_window(url, entries) -> None:
    sink = _feed_window_sink
    if sink is None:
        return
    try:
        sink(url, [str(e.id) for e in entries if getattr(e, "id", None)])
    except Exception:  # a window write must never fail a feed parse
        LOGGER.warning("feed window capture failed for %s", url, exc_info=True)


def _collect_entry_tags(url, result, entries) -> None:
    sink = _entry_tag_sink
    if sink is None:
        return
    try:
        from services.feed_tags import extract_feed_entry_tags

        # Re-derive each raw entry's reader id rather than zipping by index:
        # _process_feed skips entries it can't process, so positions may not
        # line up. reader's id derivation is `id`, with a link fallback for
        # RSS-family feeds only.
        processed_ids = {e.id for e in entries}
        is_rss = str(getattr(result, "version", "") or "").startswith("rss")
        pairs = []
        for raw_entry in result.entries:
            rid = raw_entry.get("id") or (raw_entry.get("link") if is_rss else None)
            if not rid or rid not in processed_ids:
                continue
            tags = extract_feed_entry_tags(raw_entry)
            if tags:
                pairs.append((str(rid), tags))
        if pairs:
            sink(url, pairs)
    except Exception:  # a tag write must never fail a feed parse
        LOGGER.warning("entry tag capture failed for %s", url, exc_info=True)


# Injected provider for per-feed host-rewrite rules: fn(feed_url) -> [(from,to)].
# Lets the parser rewrite an old author domain to the current one at ingest, so
# entries arrive with the current-domain id/link instead of a dead one. The
# rewrite runs on the RAW feedparser result before _process_feed, so reader's id
# derivation, tag collection, and the feed window all see the rewritten values
# consistently (rewriting after _process_feed would desync ids from the raw
# entries that tag collection matches against).
_url_rewrite_provider = None


def set_url_rewrite_provider(fn) -> None:
    global _url_rewrite_provider
    _url_rewrite_provider = fn


def _swap_host(url: str, host_map: dict[str, str]) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    bare = (parts.netloc or "").split("@")[-1].split(":")[0].lower()
    to = host_map.get(bare)
    if not to:
        return url
    return urlunsplit((parts.scheme, to, parts.path, parts.query, parts.fragment))


def _rewrite_raw_urls(url, result) -> None:
    """Rewrite old-domain hosts in the raw feedparser entries, in place."""
    if _url_rewrite_provider is None:
        return
    try:
        rules = _url_rewrite_provider(url)
    except Exception:  # noqa: BLE001
        return
    if not rules:
        return
    host_map = {str(f).lower(): str(t) for f, t in rules}
    for entry in getattr(result, "entries", None) or []:
        for key in ("id", "guid", "link"):
            val = entry.get(key)
            if isinstance(val, str) and val:
                new = _swap_host(val, host_map)
                if new != val:
                    entry[key] = new
        for link in entry.get("links", []) or []:
            href = link.get("href")
            if isinstance(href, str) and href:
                new = _swap_host(href, host_map)
                if new != href:
                    link["href"] = new


# A feed date at or before this is not a publication date, it is a parse
# failure or a placeholder the publisher never filled in. 1990 is the bound
# services/publish_date.py already uses for dates mined from a page; feed-
# supplied dates were simply never held to it.
_MIN_PUBLISH_YEAR = 1990


def _drop_placeholder_date(value):
    """None out an epoch/placeholder timestamp so it is treated as "no date"."""
    if not isinstance(value, datetime):
        return value
    dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None if dt.year <= _MIN_PUBLISH_YEAR else value


def _sanitize_entry(entry):
    """Return ``entry`` with its content/summary run through html_sanitize, and
    any placeholder publish date dropped.

    blog.gitea.com ships ``Thu, 01 Jan 1970 00:00:00 GMT`` as the pubDate on 25
    of its posts. Stored as a real timestamp, those sort above every genuine
    entry under Pub-oldest and take over the whole view — with the list showing
    "8h ago" from the received date, so nothing on screen explains why. NULL is
    already handled everywhere as "no date"; epoch is the same fact wearing a
    number, so it becomes NULL here rather than at each of the places that sort.
    """
    changed = {}
    for field in ("published", "updated"):
        raw = getattr(entry, field, None)
        cleaned = _drop_placeholder_date(raw)
        if cleaned is not raw:
            changed[field] = cleaned
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
        # Rewrite old author domains -> current, on the raw result, so reader's
        # id derivation and everything downstream see the current-domain values.
        _rewrite_raw_urls(url, result)
        feed, entries = _process_feed(url, result)
        entries = [_sanitize_entry(e) for e in entries]
        _collect_entry_tags(url, result, entries)
        _record_feed_window(url, entries)
        return feed, entries


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
