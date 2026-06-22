from __future__ import annotations

import io
import sqlite3
from collections.abc import Callable
from pathlib import Path

from reader import make_reader
from reader._storage import Storage as _ReaderStorage

from services import reader_sanitize

# Honest default identity for feed fetches — names the app + links the repo.
_HONEST_USER_AGENT = "Lectio/0.1 (+https://github.com/joshg253/Lectio)"
# Browser identity used ONLY for feeds an honest fetch was refused on (403/415/
# 429/503/hang). A full header set, not just the UA — some WAFs (e.g. nginx 415)
# sniff for Sec-Fetch-*/Accept-Language, not the UA alone. Applied via a per-feed
# request hook, never preemptively. See main.get_browser_ua_feed_urls.
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_BROWSER_HEADERS = {
    "User-Agent": _BROWSER_USER_AGENT,
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.7",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}

# Capture the original setup_db before class definition so the subclass can
# call it even when _ReaderStorage is monkeypatched in tests.
_reader_storage_setup_db = _ReaderStorage.setup_db


class _LectioReaderStorage(_ReaderStorage):
    """reader Storage subclass that tugs WAL auto-checkpoint to 200 pages
    (~800 KB) on every new connection so the WAL file never balloons to
    tens of MB between restarts."""

    @staticmethod
    def setup_db(db: sqlite3.Connection) -> None:
        _reader_storage_setup_db(db)
        try:
            db.execute("PRAGMA wal_autocheckpoint=200")
        except Exception:
            pass

# Some feeds have a leading newline (or other whitespace) before their <?xml
# declaration, which violates the XML spec and causes Python's expat parser to
# raise "XML or text declaration not at start of entity".  Some feeds also send
# Content-Type: text/html even though they are valid RSS/Atom.
#
# reader passes response.raw (a stream) directly to its parser, so we must fix
# the bytes at the stream level.  The response hook reads the stream in full,
# strips any leading whitespace, replaces response.raw with a fresh BytesIO,
# and corrects the content-type so reader routes to its feedparser.

_XML_SIGS = (b"<?xml", b"<rss", b"<feed", b"<rdf:RDF")
_HTML_SIGS = (b"<!DOCTYPE html", b"<!doctype html", b"<html", b"<HTML")


def _fix_feed_response(session, response, request, **kwargs):
    try:
        response.raw.decode_content = True
        raw_bytes = response.raw.read()
    except Exception:
        return None

    # 304 Not Modified and other no-body responses — nothing to fix.
    if not raw_bytes:
        response.raw = io.BytesIO(b"")
        return None

    # Always replace the (now-exhausted) stream so reader can still read it.
    stripped = raw_bytes.lstrip()
    is_xml = any(stripped.startswith(sig) for sig in _XML_SIGS)

    # If the server returned an HTML page instead of a feed, force the
    # Content-Type to text/html so reader's parser fails with a clear
    # "no parser for MIME type 'text/html'" error rather than silently
    # treating the HTML body as an empty or malformed feed.
    is_html = not is_xml and any(stripped[:200].upper().startswith(sig.upper()) for sig in _HTML_SIGS)

    response.raw = io.BytesIO(stripped if (is_xml or is_html) else raw_bytes)
    response._content = stripped if (is_xml or is_html) else raw_bytes

    if is_xml:
        ct = response.headers.get("Content-Type", "")
        if "html" in ct.lower():
            response.headers["Content-Type"] = "application/rss+xml"
    elif is_html:
        response.headers["Content-Type"] = "text/html"

    return None


class ReaderApi:
    """Small wrapper around python-reader client creation.

    This provides a stable seam for future extraction of reader-focused operations
    from the main FastAPI module.
    """

    def __init__(
        self,
        db_path: Path | str,
        browser_ua_provider: Callable[[], set[str]] | None = None,
    ) -> None:
        self._db_path = str(db_path)
        # Returns the set of feed URLs that should fetch with a browser identity.
        # Called live on each request (the set changes as feeds get flagged), so
        # it must be cheap; main caches it per-user.
        self._browser_ua_provider = browser_ua_provider

    def client(self):
        # Give reader's SQLite connections a 30-second busy-wait timeout so
        # user-facing writes (mark-as-read, add feed) survive background-refresh
        # write locks instead of failing immediately.
        storage = _LectioReaderStorage(self._db_path, timeout=30.0)
        # feed_root='' enables file:// URI support (absolute paths) for
        # scraped page-feeds whose XML files are written to DATA_DIR/scraped-feeds/.
        r = make_reader(self._db_path, feed_root='', _storage=storage)

        # lazy_init callbacks are popped from the END of the list (LIFO order).
        # reader's own post_init (which creates the HTTPRetriever) is registered
        # first, so it runs last.  We need our hook to run AFTER post_init so the
        # retriever exists when we try to access it.  Inserting at position 0 puts
        # our hook first in the list → it gets popped last → runs after post_init.
        def _add_response_hook(parser: object) -> None:
            for prefix in ('https://', 'http://'):
                retr = parser.retrievers.get(prefix)  # type: ignore[union-attr]
                if retr is None:
                    continue
                # FRB080/082/086: identify as Lectio, not as the underlying library.
                if hasattr(retr, 'session'):
                    retr.session.headers['User-Agent'] = _HONEST_USER_AGENT
                # Per-feed browser-identity escalation for feeds an honest fetch was
                # refused on. Runs before the request is sent and only swaps headers
                # for flagged feeds — every other feed keeps the honest UA.
                if hasattr(retr, 'request_hooks') and self._browser_ua_provider is not None:
                    retr.request_hooks.append(self._make_browser_ua_request_hook())
                if hasattr(retr, 'response_hooks'):
                    retr.response_hooks.append(_fix_feed_response)

        r._parser.lazy_init_funcs.insert(0, _add_response_hook)

        # Replace feedparser's destroy-everything sanitizer with Lectio's own
        # allowlist (keeps safe embeds: iframes from trusted hosts, SVG, MathML).
        reader_sanitize.install(r)

        return r

    def _make_browser_ua_request_hook(self):
        """Build a reader request hook that swaps in a browser identity for feeds
        the provider lists. reader calls hooks as ``hook(session, request,
        **kwargs)`` and uses the returned (or mutated) request."""
        provider = self._browser_ua_provider

        def _hook(session, request, **kwargs):
            try:
                flagged = provider() if provider else None
                if flagged and str(request.url) in flagged:
                    request.headers.update(_BROWSER_HEADERS)
            except Exception:
                pass  # never let identity selection break a fetch
            return request

        return _hook
