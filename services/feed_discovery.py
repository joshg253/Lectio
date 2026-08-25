"""RSS/Atom auto-discovery helpers."""
from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlparse

import httpx

from services import url_guard

_LOGGER = logging.getLogger(__name__)


def _guarded_get(url: str, *, timeout: float, headers: dict | None = None) -> httpx.Response:
    """SSRF-safe GET: validates the initial URL and every redirect hop.

    Raises url_guard.UnsafeURLError for private/loopback/link-local targets.
    """
    hdrs = headers or _HEADERS
    with url_guard.build_client(timeout=timeout, headers=hdrs) as client:
        return url_guard.safe_get(client, url, headers=hdrs)


def _guarded_head(url: str, *, timeout: float, headers: dict | None = None) -> httpx.Response | None:
    """SSRF-safe HEAD probe. Returns None if the URL is unsafe.

    Redirects are not followed (follow_redirects=False) so a probe can't be
    bounced to an internal address after the pre-check; a feed that only answers
    after a redirect simply isn't auto-detected via HEAD.
    """
    if not url_guard.is_safe_outbound_url(url):
        return None
    hdrs = headers or _HEADERS
    with url_guard.build_client(timeout=timeout, headers=hdrs) as client:
        return client.head(url)


# HTTP statuses that mean "refused" — escalate to a browser identity and retry.
_REFUSAL_STATUSES = frozenset({403, 415, 429, 503})
# Browser identity used ONLY after an honest fetch is refused. Full header set:
# some WAFs (nginx 415) sniff Sec-Fetch-*/Accept-Language, not just the UA.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/rss+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}


def _get_with_escalation(url: str, *, timeout: float) -> tuple[httpx.Response | None, bool]:
    """GET with good-citizen escalation: honest identity first, browser identity
    only if the honest request is *refused* (403/415/429/503) or hangs/errors.

    Returns ``(response, escalated)``. ``response`` is None only when even the
    browser retry failed to connect. ``escalated`` is True when the browser
    identity was used (so the caller can flag the feed for reader's fetch too).
    Re-raises url_guard.UnsafeURLError so SSRF blocks aren't masked.
    """
    try:
        resp = _guarded_get(url, timeout=timeout)
        if resp.status_code not in _REFUSAL_STATUSES:
            return resp, False
    except url_guard.UnsafeURLError:
        raise
    except Exception:
        resp = None  # transport error / timeout — fall through to browser retry
    try:
        return _guarded_get(url, timeout=timeout, headers=_BROWSER_HEADERS), True
    except url_guard.UnsafeURLError:
        raise
    except Exception:
        return resp, resp is not None

_LINK_RE = re.compile(r"<link\b([^>]*?)(?:/>|>)", re.IGNORECASE | re.DOTALL)
_ATTR_RE = re.compile(
    r'([a-zA-Z][a-zA-Z0-9_-]*)\s*=\s*'
    r'(?:"([^"]*)"'
    r"|'([^']*)'"
    r"|([^\s>\"'/]+))",
    re.IGNORECASE,
)

_FEED_MIME_TYPES = frozenset({
    "application/rss+xml",
    "application/atom+xml",
    "application/feed+json",
    "text/xml",
    "application/xml",
})

# Probed in order when no <link> tags are found.
_COMMON_FEED_PATHS = [
    "/feed",
    "/feed/",
    "/feed.xml",
    "/rss",
    "/rss.xml",
    "/atom.xml",
    "/atom",
    "/index.xml",
    "/feeds/posts/default",  # Blogger
]

# WordPress-style query-param variants probed against the page URL itself.
_FEED_QUERY_PARAMS = ["feed=rss2", "feed=rss", "feed=atom", "feed=json"]

# The SAME honest identity the rest of the app fetches with — deliberately
# without a parenthetical describing the activity.
#
# This used to read "Lectio/1.0 (RSS auto-discovery; +…)", and bot filters match
# on that phrase: chickensoft.games returns a fabricated 404 to any UA containing
# "RSS auto-discovery" while serving 200 to "Lectio/1.0" and to the honest UA.
# Discovery was therefore the ONE part of Lectio that could not read the site,
# and the damage was not just a failed lookup — probe_url reported "HTTP 404 —
# server denied the request", refusal_is_forceable() read that as the site
# refusing us, and Add Feed offered "Subscribe anyway". That is the husk-feed
# path the add-feed code explicitly warns against, offered because of our own
# user agent.
#
# Still honest: it names the app and links the repo. Only the description of
# what the request is for is gone, and that description was never load-bearing.
_HEADERS = {"User-Agent": "Lectio/1.0 (+https://github.com/joshg253/Lectio)"}


# --- Site-specific URL → feed rewrites -------------------------------------
# Some sites publish feeds on a separate host with no <link rel="alternate">
# on the HTML page (so generic discovery can't find them). Each rewriter takes
# the pasted URL and returns a known feed URL, or None if it doesn't apply.

# Pinboard page paths use the same segment grammar as its feed paths
# (https://pinboard.in/popular/ → https://feeds.pinboard.in/rss/popular/),
# including tag/user/source filters like /u:name/t:tag/ and private-feed
# segments like /secret:xxxx/. Segments outside this grammar (e.g. /search/,
# /settings/) mean the page has no direct feed equivalent.
_PINBOARD_SEGMENT_RE = re.compile(
    r"^(?:popular|recent|private|unread|untagged|starred|network"
    r"|[ut]:[^/]+|from:[^/]+|secret:[^/]+)$"
)


def _pinboard_feed_url(url: str) -> str | None:
    parsed = urlparse(url)
    # hostname (not netloc): strips an explicit port and lowercases.
    if (parsed.hostname or "") not in ("pinboard.in", "www.pinboard.in"):
        return None
    segments = [s for s in parsed.path.split("/") if s]
    if not segments or not all(_PINBOARD_SEGMENT_RE.match(s) for s in segments):
        return None
    return "https://feeds.pinboard.in/rss/" + "/".join(segments) + "/"


# Single-segment artstation.com paths that are site pages, not usernames — never
# rewrite these to a bogus <seg>.rss.
_ARTSTATION_RESERVED = frozenset({
    "artwork", "search", "jobs", "blogs", "prints", "marketplace", "learning",
    "contests", "channels", "guilds", "about", "terms", "privacy", "podcast",
    "magazine", "studios", "schools", "wallpapers", "2d", "3d", "login", "signup",
    "users", "explore", "following", "notifications", "messages",
})


def _artstation_feed_url(url: str) -> str | None:
    """ArtStation feeds live at ``www.artstation.com/<user>.rss``.

    The subdomain form (``<user>.artstation.com/rss``) and the profile page both
    403 any fetch — even a browser identity — but the www ``.rss`` form serves
    the feed with our honest UA. Map both profile forms onto it so Add Feed
    resolves an artist URL instead of failing on the bot-wall."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    user = None
    if host.endswith(".artstation.com") and host != "www.artstation.com":
        sub = host[:-len(".artstation.com")]
        if sub and "." not in sub:
            user = sub
    elif host in ("artstation.com", "www.artstation.com"):
        segments = [s for s in parsed.path.split("/") if s]
        if len(segments) == 1 and not segments[0].lower().endswith(".rss"):
            if segments[0].lower() not in _ARTSTATION_RESERVED:
                user = segments[0]
    if not user:
        return None
    return f"https://www.artstation.com/{user}.rss"


# Single-segment behance.net paths that are site pages, not usernames.
_BEHANCE_RESERVED = frozenset({
    "search", "galleries", "joblist", "hire", "assets", "for_you", "live",
    "onboarding", "settings", "notifications", "messages", "adobe", "blog",
    "help", "about", "careers", "login", "signup", "feeds", "gallery",
    "collection", "collections", "reviews", "schools", "discover",
})


def _behance_feed_url(url: str) -> str | None:
    """Behance per-user feeds live at ``www.behance.net/<user>.rss`` (the profile
    page itself is HTML). Map a bare profile URL onto the .rss form so Add Feed
    resolves it. (``/feeds/user?username=<user>`` also works and is left alone if
    pasted directly.)"""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in ("behance.net", "www.behance.net"):
        return None
    segments = [s for s in parsed.path.split("/") if s]
    if len(segments) != 1:
        return None
    user = segments[0]
    if user.lower().endswith(".rss") or user.lower() in _BEHANCE_RESERVED:
        return None
    return f"https://www.behance.net/{user}.rss"


def _freecodecamp_feed_url(url: str) -> str | None:
    """freeCodeCamp News (Ghost) publishes an RSS feed for every collection at
    ``<path>/rss/`` — the whole site (``/news/rss/``), a tag
    (``/news/tag/<tag>/rss/``) or an author (``/news/author/<name>/rss/``).

    Autodiscovery on a tag or author *page* advertises the site-wide feed, so
    pasting a tag page would subscribe the firehose instead of that tag (the
    reported case: ``/news/tag/advanced-mathematics/`` resolved to
    ``/news/rss``). Map the collection page to its own feed; article URLs
    (``/news/<slug>/``) have no feed and fall through to generic discovery."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in ("freecodecamp.org", "www.freecodecamp.org"):
        return None
    segments = [s for s in parsed.path.split("/") if s]
    if not segments or segments[0].lower() != "news" or segments[-1].lower() == "rss":
        return None
    if len(segments) == 1:                                   # /news/ → site feed
        path = "news"
    elif len(segments) == 3 and segments[1].lower() in ("tag", "author"):
        path = "/".join(segments)                            # /news/tag|author/<x>/
    else:
        return None                                          # article or unknown
    return f"https://www.freecodecamp.org/{path}/rss/"


_TAPAS_HOSTS = frozenset({"tapas.io", "www.tapas.io", "m.tapas.io"})


def _tapas_feed_url(url: str) -> str | None:
    """Tapas series feeds live at ``tapas.io/rss/series/<numeric id>``.

    Only the numeric form can be mapped without a fetch. A slug URL
    (``/series/club_cryptid``) carries its id in the page body instead — see
    _tapas_feed_from_body, which reads it out of the HTML discovery already
    fetched. Handling the numeric case here keeps it a zero-request rewrite.

    (``tapastic.com`` is rewritten to ``tapas.io`` by URL normalization before
    discovery ever sees it — see _DOMAIN_ALIASES in main.py.)"""
    parsed = urlparse(url)
    if (parsed.hostname or "").lower() not in _TAPAS_HOSTS:
        return None
    segments = [s for s in parsed.path.split("/") if s]
    if len(segments) != 2 or segments[0].lower() != "series" or not segments[1].isdigit():
        return None
    return f"https://tapas.io/rss/series/{segments[1]}"


# Tinyview paths that are the site, not a comic. A wrong guess is cheap — the
# rewritten URL is fetched and validated below, so a non-comic path just fails
# discovery as it would have anyway — but there is no reason to ask for
# /about/feed.rss.
_TINYVIEW_RESERVED = frozenset({
    "about", "account", "admin", "api", "blog", "comics", "contact", "discover",
    "faq", "gift", "help", "home", "login", "logout", "privacy", "search",
    "settings", "signup", "subscribe", "support", "terms", "tinyview",
})


def _tinyview_feed_url(url: str) -> str | None:
    """Tinyview comics publish at ``tinyview.com/<comic>/feed.rss``.

    The comic page returns 200 with **no** ``<link rel="alternate">`` at all —
    Tinyview renders client-side, so nothing in the served HTML advertises the
    feed and generic discovery correctly reports "no feed found" rather than
    guessing. The URL is entirely predictable, so map it.

    An episode URL (``/<comic>/2026/08/13/time``) resolves to the same comic
    feed: the first segment is the comic either way.
    """
    parsed = urlparse(url)
    if (parsed.hostname or "").lower() not in ("tinyview.com", "www.tinyview.com"):
        return None
    segments = [s for s in parsed.path.split("/") if s]
    if not segments or segments[-1].lower() == "feed.rss":
        return None
    comic = segments[0].lower()
    if comic in _TINYVIEW_RESERVED or "." in comic:
        return None
    return f"https://tinyview.com/{comic}/feed.rss"


_SITE_FEED_REWRITES = [
    _pinboard_feed_url, _artstation_feed_url, _behance_feed_url, _freecodecamp_feed_url,
    _tapas_feed_url, _tinyview_feed_url,
]

# The page's *own* series id. `seriesId: N` is a script variable that appears
# exactly once on a series page, so it is unambiguous and tried first. Episode
# pages don't carry it and fall back to data-series-id, which appears on
# recommendation cards too — hence "first match", the page's own series.
_TAPAS_SERIES_ID_RES = (
    re.compile(r"seriesId:\s*(\d+)"),
    re.compile(r'data-series-id="(\d+)"'),
)


def _tapas_feed_from_body(final_url: str, html: str) -> str | None:
    """Resolve a Tapas slug or episode URL to its series feed, from the HTML.

    Tapas advertises no ``<link rel="alternate">`` at all — its only alternate
    is the mobile page — and the canonical link points at the latest *episode*,
    not the series. So a pasted ``/series/<slug>`` URL is invisible to generic
    discovery; the series id exists only in the page body. This is the same
    trick the community userscripts use, at no extra request: discovery has
    already fetched this HTML."""
    parsed = urlparse(final_url)
    if (parsed.hostname or "").lower() not in _TAPAS_HOSTS:
        return None
    segments = [s for s in parsed.path.split("/") if s]
    if not segments or segments[0].lower() not in ("series", "episode"):
        return None
    for pattern in _TAPAS_SERIES_ID_RES:
        match = pattern.search(html)
        if match:
            return f"https://tapas.io/rss/series/{match.group(1)}"
    return None


# Run when generic autodiscovery finds no <link rel="alternate">: given the page
# HTML already in hand, return a feed URL. For sites whose feed address is
# derivable only from the body, never from the URL alone.
_SITE_BODY_FEED_EXTRACTORS = [_tapas_feed_from_body]


def feed_url_from_page_body(final_url: str, html: str) -> str | None:
    """First known-site feed URL derivable from this page's HTML, or None."""
    for extractor in _SITE_BODY_FEED_EXTRACTORS:
        try:
            found = extractor(final_url, html)
        except Exception:
            # Same contract as the URL rewriters: a bug here degrades to "no
            # feed found", it does not break Add Feed — but it is logged.
            _LOGGER.exception("body feed extractor %s failed for %r", extractor.__name__, final_url)
            continue
        if found:
            return found
    return None


def rewrite_known_site_url(url: str) -> str:
    """Map a page URL to its known feed URL for sites generic discovery can't
    handle. Returns the input unchanged when no rewriter applies."""
    for rewriter in _SITE_FEED_REWRITES:
        try:
            rewritten = rewriter(url)
        except Exception:
            # A rewriter bug must never break Add Feed (the URL just falls
            # through to generic discovery) — but don't hide it either.
            _LOGGER.exception("site feed rewriter %s failed for %r", rewriter.__name__, url)
            continue
        if rewritten:
            return rewritten
    return url


def _ct_is_feed(content_type: str) -> bool:
    base = content_type.split(";")[0].strip().lower()
    return base in _FEED_MIME_TYPES or "rss" in base or "atom" in base


_FEED_BODY_RE = re.compile(
    r"<\s*(?:rss|feed|rdf:rdf)\b",
    re.IGNORECASE,
)

def _body_is_feed(text: str) -> bool:
    """Content-sniff the first 1 KB for RSS/Atom root elements."""
    return bool(_FEED_BODY_RE.search(text[:1024]))


_MAX_PROBE_REDIRECTS = 3


def _head_following_redirects(url: str, *, timeout: float, headers: dict | None):
    """HEAD that resolves redirects one *guarded* hop at a time.

    _guarded_head deliberately refuses to follow redirects, so a probe can't be
    bounced to an internal address after the SSRF pre-check. Re-running the
    guard on every hop keeps exactly that property while letting the caller see
    where an advertised feed actually lands — which matters because a stale
    autodiscovery tag is often an ``http://`` URL whose 301 hides the 404 behind
    it (leereilly.net advertising http://leereilly.net/feed.xml, reported
    2026-07-25). Returns None when the chain is unsafe, broken, or too long,
    which callers read as "inconclusive".
    """
    seen: set[str] = set()
    for _ in range(_MAX_PROBE_REDIRECTS + 1):
        resp = _guarded_head(url, timeout=timeout, headers=headers)
        if resp is None or not resp.is_redirect:
            return resp
        location = resp.headers.get("location")
        if not location:
            return resp
        nxt = str(httpx.URL(url).join(location))
        if nxt in seen:
            return resp  # redirect loop — treat as inconclusive
        seen.add(nxt)
        url = nxt
    return None


def _advertised_feed_dead(url: str, *, headers: dict | None) -> bool:
    """Positively confirm an advertised feed URL is broken (stale autodiscovery
    tag — sites move a feed and leave the old <link rel="alternate"> behind,
    e.g. dropmark.com advertising /rss while the feed lives at /feed.xml).

    Conservative on purpose: an advertised link is only discarded when a HEAD
    comes back 4xx/5xx with the current identity AND after a browser-identity
    retry. 405/501 (HEAD-hostile servers), network errors, and SSRF-blocked URLs
    all keep the link.

    Redirects are *followed* (guarded, one hop at a time) rather than keeping
    the link outright: a stale tag is often an ``http://`` URL, and stopping at
    its 301 hid the 404 behind it — so discovery offered a feed that the add
    then refused, with no way for the user to tell which was right."""
    return _advertised_feed_status(url, headers=headers) is not None


# "The feed is not there" — as opposed to "the server refused to tell us"
# (401/403/429/5xx), which a real GET from reader may well get past. Only the
# former is certain enough to stop offering the link at all.
_FEED_GONE_STATUSES = frozenset({404, 410})


def _advertised_feed_status(url: str, *, headers: dict | None) -> int | None:
    """Confirmed failing status for an advertised feed URL, else None.

    None means "inconclusive, treat the link as live" — a 405/501 HEAD-hostile
    server, a redirect loop, a network error, or an SSRF-blocked target. A
    number means both the current identity *and* a browser-identity retry
    agreed the URL fails, and its value lets the caller separate *gone* (404,
    410) from *refused* (403 and friends).
    """
    def _confirms_dead(resp) -> bool:
        return resp is not None and resp.status_code >= 400 and resp.status_code not in (405, 501)

    try:
        head = _head_following_redirects(url, timeout=3.0, headers=headers)
        if head is None or not _confirms_dead(head):
            return None
        if headers is _BROWSER_HEADERS:
            return int(head.status_code)
        retry = _head_following_redirects(url, timeout=3.0, headers=_BROWSER_HEADERS)
        return int(retry.status_code) if _confirms_dead(retry) else None
    except Exception:
        return None


def _probe_conventional_paths(final_url: str, *, headers: dict | None) -> list[dict]:
    """HEAD-probe the conventional feed paths — relative to the page path first,
    then from the site root. Returns the first hit as [{"url", "title"}].

    Page path first because the *more specific* feed is the one the user asked
    for. Multisite WordPress puts a whole blog under a path
    (devblogs.microsoft.com/oldnewthing/) while the root serves a firehose of
    every blog on the domain; probing the root first meant subscribing to
    "The Old New Thing" silently handed back "Microsoft for Developers".
    A page path with no feed of its own still falls through to the root.
    """
    parsed = urlparse(final_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    page_dir = parsed.path.rstrip("/")
    prefixes = ([page_dir] if page_dir else []) + [""]
    for prefix in prefixes:
        for suffix in _COMMON_FEED_PATHS:
            probe = origin + prefix + suffix
            try:
                head = _guarded_head(probe, timeout=3.0, headers=headers)
                if head is not None and head.is_success and _ct_is_feed(head.headers.get("content-type", "")):
                    return [{"url": str(head.url), "title": None}]
            except Exception:
                continue
    return []


def _parse_attrs(tag_body: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for m in _ATTR_RE.finditer(tag_body):
        key = m.group(1).lower()
        val = next((g for g in (m.group(2), m.group(3), m.group(4)) if g is not None), "")
        attrs.setdefault(key, val)
    return attrs


def probe_url(url: str, *, timeout: float = 10.0) -> dict:
    """Probe a URL and return structured feed discovery results for the Add Feed dialog.

    Returns a dict with keys:
      status: "feed" | "feeds" | "none" | "blocked" | "error"
      feeds:  list of {"url": str, "title": str | None}
      message: str (human-readable, empty on success)
    """
    rewritten = rewrite_known_site_url(url)
    was_rewritten = rewritten != url
    url = rewritten
    try:
        resp, _escalated = _get_with_escalation(url, timeout=timeout)
    except url_guard.UnsafeURLError:
        # "unsafe" marks the one refusal the user may NOT override. Both
        # refusal shapes report status "blocked" (see the bot-protection branch
        # below), so anything offering a force-subscribe must tell them apart —
        # use refusal_is_forceable() rather than reading status directly.
        return {"status": "blocked", "reason": "unsafe", "feeds": [],
                "message": "That address is not allowed (private/loopback target)."}
    except httpx.TimeoutException:
        return {"status": "error", "feeds": [], "message": "Connection timed out."}
    except Exception as exc:
        # Only the exception class reaches the client (CodeQL: exception text
        # can leak internal details); the full story goes to the log.
        _LOGGER.debug("probe failed for %s", url, exc_info=True)
        return {"status": "error", "feeds": [], "message": f"Could not reach URL ({type(exc).__name__})."}
    if resp is None:
        return {"status": "error", "feeds": [], "message": "Could not reach URL."}
    _probe_headers = _BROWSER_HEADERS if _escalated else _HEADERS

    final_url = str(resp.url)
    ct = resp.headers.get("content-type", "")
    body_len = len(resp.content)

    if resp.is_success and (_ct_is_feed(ct) or _body_is_feed(resp.text)):
        if was_rewritten:
            # The pasted URL was a page mapped to a known feed host — not a
            # "direct feed URL". Return it as a discovered feed so the dialog
            # shows the resolved address instead of claiming it was pasted.
            return {"status": "feed", "feeds": [{"url": final_url, "title": None}], "message": ""}
        return {"status": "feed", "feeds": [{"url": final_url, "title": None}], "message": "", "direct": True}

    if not resp.is_success:
        # The pasted URL itself is dead — often a stale advertised feed URL
        # (dropmark.com/rss) whose feed simply moved. Probe the conventional
        # paths on the same origin before giving up.
        fallback = _probe_conventional_paths(final_url, headers=_probe_headers)
        if fallback:
            return {"status": "feed", "feeds": fallback, "message": ""}
        return {"status": "error", "feeds": [], "message": f"HTTP {resp.status_code} — server denied the request."}

    # 2xx but suspiciously empty HTML → bot protection / challenge page
    if body_len < 512 and "html" in ct.lower():
        code_note = f" (HTTP {resp.status_code})" if resp.status_code != 200 else ""
        return {
            "status": "blocked",
            "feeds": [],
            "message": (
                f"The site returned an empty response{code_note}, likely blocking automated "
                "access (e.g. Cloudflare bot protection). Try pasting the direct feed URL "
                "if you know it, or subscribe as a Page Feed."
            ),
        }

    # Parse <link rel="alternate"> tags from the HTML; preserve declaration order.
    feeds: list[dict] = []
    seen: set[str] = set()
    for m in _LINK_RE.finditer(resp.text[:200_000]):
        attrs = _parse_attrs(m.group(1))
        if "alternate" not in attrs.get("rel", "").lower():
            continue
        mtype = attrs.get("type", "").split(";")[0].strip().lower()
        href = attrs.get("href", "").strip()
        if mtype not in _FEED_MIME_TYPES or not href:
            continue
        absolute = urljoin(final_url, href)
        if absolute in seen:
            continue
        seen.add(absolute)
        feeds.append({"url": absolute, "title": attrs.get("title", "").strip() or None})

    if not feeds:
        # Nothing advertised: some sites hide the feed address in the page body
        # (Tapas). Falls through to the liveness check below like any other
        # candidate, so a stale id is caught rather than offered.
        from_body = feed_url_from_page_body(final_url, resp.text[:200_000])
        if from_body:
            feeds.append({"url": from_body, "title": None})

    stale_advertised: list[dict] = []
    gone_advertised: list[dict] = []
    if feeds:
        live: list[dict] = []
        for f in feeds:
            status = _advertised_feed_status(f["url"], headers=_probe_headers)
            if status is None:
                live.append(f)
            elif status in _FEED_GONE_STATUSES:
                gone_advertised.append(f)
            else:
                # Refused rather than absent (403 bot-wall, 5xx): reader's real
                # GET may still succeed, so this stays a last resort.
                stale_advertised.append(f)
        if live:
            return {
                "status": "feed" if len(live) == 1 else "feeds",
                "feeds": live,
                "message": "",
            }
        _LOGGER.info("discovery: all %d advertised feed link(s) on %s look dead; probing conventional paths",
                     len(feeds), final_url)

    # Probe common path suffixes: first from the site root, then relative to the page path.
    path_hit = _probe_conventional_paths(final_url, headers=_probe_headers)
    if path_hit:
        return {"status": "feed", "feeds": path_hit, "message": ""}
    parsed = urlparse(final_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    page_dir = parsed.path.rstrip("/")

    # Probe WordPress-style query-param variants — collect ALL matches so the picker
    # can show every format option (rss2 / rss / atom may all coexist).
    qp_feeds: list[dict] = []
    if page_dir:
        base_page = f"{origin}{page_dir}/"
        for qp in _FEED_QUERY_PARAMS:
            probe = f"{base_page}?{qp}"
            try:
                head = _guarded_head(probe, timeout=3.0, headers=_probe_headers)
                if head is not None and head.is_success and _ct_is_feed(head.headers.get("content-type", "")):
                    resolved = str(head.url)
                    if not any(f["url"] == resolved for f in qp_feeds):
                        qp_feeds.append({"url": resolved, "title": None})
            except Exception:
                continue

    if qp_feeds:
        return {
            "status": "feed" if len(qp_feeds) == 1 else "feeds",
            "feeds": qp_feeds,
            "message": "",
        }

    if stale_advertised:
        # Nothing better found, and these were *refused* rather than absent —
        # surface them so a bot-walled site can still be subscribed.
        return {
            "status": "feed" if len(stale_advertised) == 1 else "feeds",
            "feeds": stale_advertised,
            "message": "",
        }

    if gone_advertised:
        # The site advertises a feed that is provably gone (404/410 through the
        # whole redirect chain, under both identities) and nothing else answers.
        # Offering it would hand back a URL the add route then rejects — which
        # reads as "it found my feed" followed by a feed that isn't there.
        # Common on static-site blogs whose old posts still carry the tag.
        return {
            "status": "none",
            "feeds": [],
            "message": (
                f"This site advertises a feed at {gone_advertised[0]['url']}, but that address "
                "is gone (404) and no other feed answers. Subscribe as a Page Feed instead."
            ),
        }

    return {"status": "none", "feeds": [], "message": "No RSS/Atom feed found at this URL."}


# Hosts known to serve the origin site's own homepage back at the feed URL
# (wrong content-type, no redirect) once a feed dies, rather than 404ing —
# so probing the feed URL itself just finds a circular <link rel="alternate">
# pointing at itself. The origin is recoverable from the page's own
# <link rel="canonical">, which FeedBurner's passthrough leaves untouched.
_DEAD_END_FEED_HOSTS = {"feeds.feedburner.com", "feeds2.feedburner.com"}


def is_known_dead_end_host(feed_url: str) -> bool:
    """Whether suggest_feed_migration has any chance of finding a candidate for
    this feed — lets a UI gate the "Suggest fix" affordance to feeds it's
    actually worth probing."""
    return (urlparse(feed_url).hostname or "").lower() in _DEAD_END_FEED_HOSTS


def suggest_feed_migration(feed_url: str, *, timeout: float = 10.0) -> dict:
    """Find a live replacement for a feed hosted on a known dead-end service.

    Currently FeedBurner only. Reads the origin site's URL out of the page
    FeedBurner now serves (its <link rel="canonical">), then runs normal
    discovery there — reusing probe_url so the candidate is verified the same
    way Change Feed URL already verifies one, not just guessed at.

    Returns the same shape as probe_url: {"status", "feeds", "message"}.
    A candidate is only present when "feeds" is non-empty; every other field
    combination means "no suggestion," not an error the caller must branch on.
    """
    host = (urlparse(feed_url).hostname or "").lower()
    if host not in _DEAD_END_FEED_HOSTS:
        return {"status": "none", "feeds": [], "message": "No known migration for this feed's host."}
    try:
        resp, _escalated = _get_with_escalation(feed_url, timeout=timeout)
    except url_guard.UnsafeURLError:
        return {"status": "blocked", "feeds": [], "message": "That address is not allowed."}
    except Exception:
        return {"status": "error", "feeds": [], "message": "Could not reach the feed URL."}
    if resp is None or not resp.is_success:
        return {"status": "error", "feeds": [], "message": "Could not reach the feed URL."}

    canonical: str | None = None
    for m in _LINK_RE.finditer(resp.text[:200_000]):
        attrs = _parse_attrs(m.group(1))
        if attrs.get("rel", "").strip().lower() == "canonical" and attrs.get("href", "").strip():
            canonical = urljoin(str(resp.url), attrs["href"].strip())
            break
    if not canonical:
        return {"status": "none", "feeds": [],
                "message": "No canonical link on the page FeedBurner is serving — nothing to follow."}
    canonical_host = (urlparse(canonical).hostname or "").lower()
    if not canonical_host or canonical_host in _DEAD_END_FEED_HOSTS:
        return {"status": "none", "feeds": [], "message": "The canonical link doesn't lead off FeedBurner."}
    return probe_url(canonical, timeout=timeout)


def discover_feed_urls(url: str, *, timeout: float = 10.0) -> list[str]:
    """Return RSS/Atom feed URLs reachable from url. See discover_feed_urls_ex."""
    return discover_feed_urls_ex(url, timeout=timeout)[0]


def refusal_is_forceable(probe: dict) -> bool:
    """May the user subscribe to this URL anyway, despite the failed probe?

    Yes for a REFUSAL — a 403, a timeout, an anti-bot challenge page. We never
    saw the content, so the address may well be a real feed behind a wall that
    starts working later.

    No for two cases that look similar and are not:
      - the SSRF guard's refusal (``reason == "unsafe"``). Overriding it would
        let a private/loopback address be subscribed, and the force path skips
        discovery, so this probe is the only thing standing there.
      - a page we fetched FINE that simply has no feed (``status == "none"``).
        Subscribing to that produces a husk: a permanently failing "feed"
        holding whatever gets captured onto it. 29 of those accumulated before
        the distinction existed (scripts/rehome_article_feeds.py).
    """
    if probe.get("reason") == "unsafe":
        return False
    return str(probe.get("status") or "") in ("error", "blocked")


def discover_feed_urls_ex(url: str, *, timeout: float = 10.0) -> tuple[list[str], bool]:
    """Like discover_feed_urls but also reports whether a browser identity was
    needed (the honest fetch was refused). Returns ``(urls, escalated)``.

    If a feed was only reachable with a browser identity, the caller should flag
    it so reader's later refresh fetch escalates too (otherwise the feed
    subscribes but never updates).
    """
    url = rewrite_known_site_url(url)
    try:
        resp, escalated = _get_with_escalation(url, timeout=timeout)
    except Exception:
        return [], False
    if resp is None or not resp.is_success:
        return [], escalated
    probe_headers = _BROWSER_HEADERS if escalated else _HEADERS

    final_url = str(resp.url)
    ct = resp.headers.get("content-type", "")

    if _ct_is_feed(ct) or _body_is_feed(resp.text):
        return [final_url], escalated

    # Parse HTML <link rel="alternate"> tags; preserve declaration order.
    candidates: list[str] = []
    for m in _LINK_RE.finditer(resp.text):
        attrs = _parse_attrs(m.group(1))
        rel = attrs.get("rel", "").lower()
        mtype = attrs.get("type", "").split(";")[0].strip().lower()
        href = attrs.get("href", "").strip()
        if "alternate" in rel and mtype in _FEED_MIME_TYPES and href:
            absolute = urljoin(final_url, href)
            if absolute not in candidates:
                candidates.append(absolute)

    if not candidates:
        # Feed address hidden in the page body (Tapas) — same call, same slice
        # of the same HTML as probe_url, so the two cannot disagree.
        from_body = feed_url_from_page_body(final_url, resp.text[:200_000])
        if from_body:
            candidates.append(from_body)

    # This mirrors probe_url's rules deliberately: probe_url previews what the
    # Add dialog shows, but the Add route itself re-discovers through here, so
    # any divergence means the dialog promises one feed and the button
    # subscribes to another. Both the page-path-before-root ordering and the
    # gone/refused split therefore run off the same helpers.
    stale_candidates: list[str] = []
    if candidates:
        live: list[str] = []
        for c in candidates:
            status = _advertised_feed_status(c, headers=probe_headers)
            if status is None:
                live.append(c)
            elif status not in _FEED_GONE_STATUSES:
                stale_candidates.append(c)  # refused, not absent — last resort
        if live:
            return live, escalated
        candidates = []

    parsed = urlparse(final_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    page_dir = parsed.path.rstrip("/")
    path_hit = _probe_conventional_paths(final_url, headers=probe_headers)
    if path_hit:
        for hit in path_hit:
            if hit["url"] not in candidates:
                candidates.append(hit["url"])
        return candidates, escalated

    # Also try WordPress-style query-param variants on the page URL itself.
    if page_dir:
        base_page = f"{origin}{page_dir}/"
        for qp in _FEED_QUERY_PARAMS:
            probe_candidate = f"{base_page}?{qp}"
            try:
                head = _guarded_head(probe_candidate, timeout=3.0, headers=probe_headers)
                if head is not None and head.is_success and _ct_is_feed(head.headers.get("content-type", "")):
                    resolved = str(head.url)
                    if resolved not in candidates:
                        candidates.append(resolved)
                    return candidates, escalated
            except Exception:
                continue

    return candidates or stale_candidates, escalated
