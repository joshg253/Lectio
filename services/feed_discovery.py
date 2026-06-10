"""RSS/Atom auto-discovery helpers."""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

import httpx

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
_FEED_QUERY_PARAMS = ["feed=rss2", "feed=rss", "feed=atom"]

_HEADERS = {"User-Agent": "Lectio/1.0 (RSS auto-discovery; +https://github.com/joshg253/Lectio)"}


def _ct_is_feed(content_type: str) -> bool:
    base = content_type.split(";")[0].strip().lower()
    return base in _FEED_MIME_TYPES or "rss" in base or "atom" in base


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
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True, headers=_HEADERS)
    except httpx.TimeoutException:
        return {"status": "error", "feeds": [], "message": "Connection timed out."}
    except Exception as exc:
        short = str(exc).split("\n")[0][:160]
        return {"status": "error", "feeds": [], "message": f"Could not reach URL: {short}"}

    final_url = str(resp.url)
    ct = resp.headers.get("content-type", "")
    body_len = len(resp.content)

    if resp.is_success and _ct_is_feed(ct):
        return {"status": "feed", "feeds": [{"url": final_url, "title": None}], "message": "", "direct": True}

    if not resp.is_success:
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

    if feeds:
        return {
            "status": "feed" if len(feeds) == 1 else "feeds",
            "feeds": feeds,
            "message": "",
        }

    # Probe common path suffixes: first from the site root, then relative to the page path.
    parsed = urlparse(final_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    page_dir = parsed.path.rstrip("/")
    prefixes = [""] + ([page_dir] if page_dir else [])
    for prefix in prefixes:
        for suffix in _COMMON_FEED_PATHS:
            probe = origin + prefix + suffix
            try:
                head = httpx.head(probe, timeout=3.0, follow_redirects=True, headers=_HEADERS)
                if head.is_success and _ct_is_feed(head.headers.get("content-type", "")):
                    return {"status": "feed", "feeds": [{"url": str(head.url), "title": None}], "message": ""}
            except Exception:
                continue

    # Probe WordPress-style query-param variants — collect ALL matches so the picker
    # can show every format option (rss2 / rss / atom may all coexist).
    qp_feeds: list[dict] = []
    if page_dir:
        base_page = f"{origin}{page_dir}/"
        for qp in _FEED_QUERY_PARAMS:
            probe = f"{base_page}?{qp}"
            try:
                head = httpx.head(probe, timeout=3.0, follow_redirects=True, headers=_HEADERS)
                if head.is_success and _ct_is_feed(head.headers.get("content-type", "")):
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

    return {"status": "none", "feeds": [], "message": "No RSS/Atom feed found at this URL."}


def discover_feed_urls(url: str, *, timeout: float = 10.0) -> list[str]:
    """Return RSS/Atom feed URLs reachable from url.

    If url itself is a feed, returns [url].
    If url is an HTML page, parses <link rel="alternate"> tags and probes
    common path suffixes.
    Returns [] on network failure or when nothing is found.
    """
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True, headers=_HEADERS)
    except Exception:
        return []

    if not resp.is_success:
        return []

    final_url = str(resp.url)
    ct = resp.headers.get("content-type", "")

    if _ct_is_feed(ct):
        return [final_url]

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

    if candidates:
        return candidates

    # Probe common path suffixes: first from the site root, then relative to the page path.
    parsed = urlparse(final_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    page_dir = parsed.path.rstrip("/")
    prefixes = [""] + ([page_dir] if page_dir else [])
    for prefix in prefixes:
        for suffix in _COMMON_FEED_PATHS:
            probe_candidate = origin + prefix + suffix
            try:
                head = httpx.head(probe_candidate, timeout=3.0, follow_redirects=True, headers=_HEADERS)
                if head.is_success and _ct_is_feed(head.headers.get("content-type", "")):
                    resolved = str(head.url)
                    if resolved not in candidates:
                        candidates.append(resolved)
                    return candidates
            except Exception:
                continue

    # Also try WordPress-style query-param variants on the page URL itself.
    if page_dir:
        base_page = f"{origin}{page_dir}/"
        for qp in _FEED_QUERY_PARAMS:
            probe_candidate = f"{base_page}?{qp}"
            try:
                head = httpx.head(probe_candidate, timeout=3.0, follow_redirects=True, headers=_HEADERS)
                if head.is_success and _ct_is_feed(head.headers.get("content-type", "")):
                    resolved = str(head.url)
                    if resolved not in candidates:
                        candidates.append(resolved)
                    return candidates
            except Exception:
                continue

    return candidates
