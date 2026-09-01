from __future__ import annotations

import io
import json
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict

from reader import make_reader
from reader._storage import Storage as _ReaderStorage

from services import bot_challenge, flaresolverr, reader_sanitize


class _ExtraReaderKwargs(TypedDict, total=False):
    session_timeout: tuple[float, float]

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
            # Wait (up to 10s) for a lock instead of failing instantly with
            # "database is locked" — matches the meta connections. Concurrent
            # writers (background refresh, FTS index build, startup backfills)
            # briefly contend the reader DB; without this a losing opener errors
            # out (a recurring flaky-CI signature) instead of retrying.
            db.execute("PRAGMA busy_timeout=10000")
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


# XML 1.0 permits only #x9 (tab), #xA (LF), #xD (CR) from the C0 range; every
# other control byte is forbidden and makes a document not well-formed. Matched
# on bytes rather than text because the body is scrubbed before decoding.
_XML_ILLEGAL_BYTES_RE = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _fix_flaresolverr_response(session, response, request, **kwargs):
    """Unwrap a FlareSolverr /v1 response back into what a normal feed fetch
    would have returned, so everything downstream (starting with
    _fix_feed_response, registered right after this one) treats it exactly
    like one. Only touches requests the flaresolverr request hook actually
    redirected — see the _lectio_via_flaresolverr marker it sets.

    Clears the marker immediately once it fires: reader's lazy_init_funcs
    fires _add_response_hook twice per retriever in practice (confirmed
    empirically 2026-08-30, same as the request hook's own note), registering
    two copies of this function. Without clearing, the second copy would
    re-run on the ALREADY-unwrapped plain feed bytes from the first and try
    to json.loads() real XML."""
    if not getattr(request, "_lectio_via_flaresolverr", False):
        return None
    request._lectio_via_flaresolverr = False
    try:
        response.raw.decode_content = True
        raw_bytes = response.raw.read()
        data = json.loads(raw_bytes)
    except Exception:
        return None  # not valid JSON — let it fail downstream as a normal HTTP/parse error

    # FeedBlockedError propagates uncaught if FlareSolverr itself gave up.
    solution = flaresolverr.parse_envelope(data, getattr(request, "url", "") or "")

    unwrapped = flaresolverr.unwrap_view_source(solution.html)
    if unwrapped is not None:
        content = unwrapped
        content_type = "application/rss+xml"
    else:
        # Not the XML-viewer wrapper — origin served real HTML (still a block
        # page, a login wall, whatever). Pass it through as-is so
        # _fix_feed_response's own HTML/challenge detection gets a real look
        # at it, rather than guessing here.
        content = solution.html.encode("utf-8")
        content_type = "text/html"

    response.raw = io.BytesIO(content)
    response._content = content
    # FlareSolverr's own response is application/json — left as-is, reader
    # would route the (now-unwrapped) body to its JSON-feed parser instead of
    # feedparser and fail with an empty-document JSON error. Set here rather
    # than left to _fix_feed_response's own sniffing: that only overrides
    # Content-Type when it sees "html" in the CURRENT value (the case it was
    # built for — a feed mistakenly served as text/html), which never matches
    # "application/json".
    response.headers["Content-Type"] = content_type
    # The origin's real status, not FlareSolverr's own 200 — so refusal
    # detection upstream still works for a site that's STILL blocking even
    # through a real browser (this isn't a magic bypass, just a real one).
    if solution.status is not None:
        response.status_code = solution.status
    return None


def _fix_feed_response(session, response, request, **kwargs):
    try:
        response.raw.decode_content = True
        raw_bytes = response.raw.read()
    except Exception:
        return None

    # A header-only challenge (AWS WAF's x-amzn-waf-action, served with a
    # 0-byte body on kcls.org) has to be checked before the no-body return
    # below, or an empty challenge response looks identical to a genuine
    # 304/no-body response and is silently swallowed instead of labeled.
    _header_challenge = bot_challenge.detect_challenge_headers(response.headers)
    if _header_challenge:
        response.raw = io.BytesIO(raw_bytes)
        response._content = raw_bytes
        raise bot_challenge.FeedBlockedError(_header_challenge, getattr(request, "url", "") or "")

    # 304 Not Modified and other no-body responses — nothing to fix.
    if not raw_bytes:
        response.raw = io.BytesIO(b"")
        return None

    # Strip control characters XML 1.0 forbids outright. reader's parser is a
    # strict SAX parser, so ONE stray byte kills the whole document: a raw 0x0B
    # sitting mid-sentence in a post ("…interesting talk.\x0bHi. I'm Al…") made
    # inventwithpython.com's 2.7MB feed unparseable in its entirety, reported as
    # "not well-formed (invalid token)" at line 19918. feedparser is lenient and
    # reads the same feed happily, which is exactly why this is worth doing here
    # rather than assuming a feed that "looks fine" will ingest.
    #
    # Only the characters that are ILLEGAL in XML are removed — tab, newline and
    # carriage return are explicitly kept, so nothing legitimate is touched and a
    # feed that was already valid comes through byte-identical.
    if _XML_ILLEGAL_BYTES_RE.search(raw_bytes):
        raw_bytes = _XML_ILLEGAL_BYTES_RE.sub(b"", raw_bytes)

    # An anti-bot challenge served in place of the feed is a *block*, not a
    # malformed feed, and the two want opposite fixes. Raised here rather than
    # left to fail as a parse error so the recorded failure says so — see
    # services.bot_challenge for why the status code cannot be used to tell them
    # apart (SiteGround serves its captcha as a 202).
    _challenge = bot_challenge.detect_challenge(
        response.headers.get("Content-Type"), raw_bytes
    )
    if _challenge:
        response.raw = io.BytesIO(raw_bytes)
        response._content = raw_bytes
        raise bot_challenge.FeedBlockedError(_challenge, getattr(request, "url", "") or "")

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
        proxy_resolver: Callable[[str], str | None] | None = None,
        flaresolverr_resolver: Callable[[str], tuple[str, str | None] | None] | None = None,
        session_timeout: tuple[float, float] | None = None,
    ) -> None:
        self._db_path = str(db_path)
        # Returns the set of feed URLs that should fetch with a browser identity.
        # Called live on each request (the set changes as feeds get flagged), so
        # it must be cheap; main caches it per-user.
        self._browser_ua_provider = browser_ua_provider
        # Given a feed URL, returns the proxy URL to fetch it through, or None
        # for a direct fetch. Called live on every request (mode/flag state can
        # change between fetches); main resolves settings + the as-needed flag
        # set and keeps this cheap. Returns None for a feed the flaresolverr
        # resolver below is handling instead — the two must never both apply
        # to the same request, or the FlareSolverr call itself would get
        # routed through the primary proxy's session.proxies by mistake.
        self._proxy_resolver = proxy_resolver
        # Given a feed URL, returns (flaresolverr_endpoint_url, proxy_url_to_
        # stack_or_None) or None for no escalation. One rung past proxy_resolver:
        # only ever consulted for a feed the primary proxy already failed on. The
        # stack proxy is embedded in FlareSolverr's own request body (its `proxy`
        # field) — not the same thing as this session's own session.proxies.
        self._flaresolverr_resolver = flaresolverr_resolver
        # (connect, read) seconds for every feed fetch. Passed through to reader's
        # requests session. None keeps reader's own default.
        self._session_timeout = session_timeout

    def client(self):
        # Give reader's SQLite connections a 30-second busy-wait timeout so
        # user-facing writes (mark-as-read, add feed) survive background-refresh
        # write locks instead of failing immediately.
        storage = _LectioReaderStorage(self._db_path, timeout=30.0)
        # feed_root='' enables file:// URI support (absolute paths) for
        # scraped page-feeds whose XML files are written to DATA_DIR/scraped-feeds/.
        # plugins=[] disables the default .ua_fallback plugin — Lectio's own per-feed
        # browser-UA escalation (full header set, not just UA) is a strict superset.
        # .entry_dedupe merges user state when feed entry IDs change (slug rewrites,
        # CMS migrations). .enclosure_dedupe drops duplicate enclosure URLs per entry.
        #
        # session_timeout is stated explicitly rather than left to reader's default
        # (3.05, 60): the scheduler refreshes feeds strictly sequentially, so every
        # second a single unresponsive host holds is a second every feed behind it
        # waits. It is a per-socket-read deadline, not a total one — a host that
        # trickles bytes can still outlast it, which is what the scheduler watchdog
        # in main.py is for.
        extra: _ExtraReaderKwargs = (
            {} if self._session_timeout is None else {"session_timeout": self._session_timeout}
        )
        r = make_reader(
            self._db_path,
            feed_root='',
            _storage=storage,
            plugins=['.entry_dedupe', '.enclosure_dedupe'],
            **extra,
        )

        # lazy_init callbacks are popped from the END of the list (LIFO order).
        # reader's own post_init (which creates the HTTPRetriever) is registered
        # first, so it runs last.  We need our hook to run AFTER post_init so the
        # retriever exists when we try to access it.  Inserting at position 0 puts
        # our hook first in the list → it gets popped last → runs after post_init.
        def _add_response_hook(parser: object) -> None:
            for prefix in ('https://', 'http://'):
                retr = parser.retrievers.get(prefix)  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]
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
                if hasattr(retr, 'request_hooks') and self._proxy_resolver is not None:
                    retr.request_hooks.append(self._make_proxy_request_hook())
                # Last among request hooks: for a flagged feed it replaces the
                # request outright (method/url/body), so it must have final say
                # over whatever the browser-UA/proxy hooks did to it first.
                if hasattr(retr, 'request_hooks') and self._flaresolverr_resolver is not None:
                    retr.request_hooks.append(self._make_flaresolverr_request_hook())
                if hasattr(retr, 'response_hooks'):
                    # Must run BEFORE _fix_feed_response: it unwraps FlareSolverr's
                    # JSON envelope back into plain feed bytes, which _fix_feed_response
                    # then cleans up exactly like any other fetch's response.
                    retr.response_hooks.append(_fix_flaresolverr_response)
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

    def _make_proxy_request_hook(self):
        """Build a reader request hook that routes fetches through a proxy per
        the resolver's per-URL decision. requests has no per-request proxies
        parameter reachable from a hook, only the session-wide default it reads
        at send() time — so this mutates ``session.proxies`` directly, right
        before send. Safe because the scheduler refreshes feeds strictly
        sequentially (see the session_timeout docstring above), so nothing else
        touches this session's proxies concurrently."""
        resolver = self._proxy_resolver

        def _hook(session, request, **kwargs):
            proxy_url = None
            try:
                proxy_url = resolver(str(request.url)) if resolver else None
            except Exception:
                pass  # never let proxy selection break a fetch
            if hasattr(session, 'proxies'):
                session.proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else {}
            return request

        return _hook

    def _make_flaresolverr_request_hook(self):
        """Build a reader request hook that redirects a flagged feed's fetch to
        FlareSolverr's /v1 endpoint instead of the feed's own URL — a real
        headless-Chrome challenge solve, not a proxy swap, so the whole request
        (method/url/body) is replaced rather than just headers or session.proxies.
        _fix_flaresolverr_response (a response hook) unwraps the reply back into
        plain feed bytes; the _lectio_via_flaresolverr marker set here is what
        tells it which responses are its to unwrap.

        Self-guarding on that same marker: reader's lazy_init_funcs fires
        _add_response_hook twice per retriever in practice (confirmed
        empirically 2026-08-30 — each call appends another copy of this hook,
        so it runs twice per fetch), and unlike the browser-UA/proxy hooks
        this one is NOT naturally idempotent — it mutates request.url, so a
        second pass over an already-redirected request read the FlareSolverr
        endpoint itself as "the feed to fetch" and asked FlareSolverr to visit
        its own URL. The other two hooks only mutate headers/session.proxies
        off the ORIGINAL request each time, which stays correct however many
        times they run."""
        resolver = self._flaresolverr_resolver

        def _hook(session, request, **kwargs):
            if getattr(request, "_lectio_via_flaresolverr", False):
                return request
            try:
                original_url = str(request.url)
                resolved = resolver(original_url) if resolver else None
            except Exception:
                resolved = None  # never let backend selection break a fetch
            if not resolved:
                return request
            flaresolverr_url, stack_proxy_url = resolved
            body = flaresolverr.build_request_body(original_url, proxy_url=stack_proxy_url)
            request.method = "POST"
            request.url = flaresolverr_url
            request.headers = {"Content-Type": "application/json"}
            request.data = json.dumps(body).encode("utf-8")
            request._lectio_via_flaresolverr = True
            return request

        return _hook
