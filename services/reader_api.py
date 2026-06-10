from __future__ import annotations

import io
from pathlib import Path

import sqlite3

from reader import make_reader
from reader._storage import Storage as _ReaderStorage

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

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = str(db_path)

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
                    retr.session.headers['User-Agent'] = (
                        'Lectio/0.1 (+https://github.com/joshg253/Lectio)'
                    )
                if hasattr(retr, 'response_hooks'):
                    retr.response_hooks.append(_fix_feed_response)

        r._parser.lazy_init_funcs.insert(0, _add_response_hook)

        return r
