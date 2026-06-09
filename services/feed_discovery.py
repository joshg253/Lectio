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

# Probed in order when no <link> tags are found. Atom paths come first.
_COMMON_FEED_PATHS = [
    "/atom.xml",
    "/atom",
    "/feed",
    "/feed/",
    "/rss",
    "/rss.xml",
    "/feed.xml",
    "/index.xml",
    "/feeds/posts/default",  # Blogger
]

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
        return {"status": "feed", "feeds": [{"url": final_url, "title": None}], "message": ""}

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

    # Parse <link rel="alternate"> tags from the HTML
    atom_feeds: list[dict] = []
    other_feeds: list[dict] = []
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
        entry = {"url": absolute, "title": attrs.get("title", "").strip() or None}
        (atom_feeds if mtype == "application/atom+xml" else other_feeds).append(entry)

    candidates = atom_feeds + other_feeds
    if candidates:
        return {
            "status": "feed" if len(candidates) == 1 else "feeds",
            "feeds": candidates,
            "message": "",
        }

    # Probe common path suffixes
    parsed = urlparse(final_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    for suffix in _COMMON_FEED_PATHS:
        probe = origin + suffix
        try:
            head = httpx.head(probe, timeout=5.0, follow_redirects=True, headers=_HEADERS)
            if head.is_success and _ct_is_feed(head.headers.get("content-type", "")):
                return {"status": "feed", "feeds": [{"url": str(head.url), "title": None}], "message": ""}
        except Exception:
            continue

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

    # Parse HTML <link rel="alternate"> tags, keeping Atom ahead of RSS.
    atom_candidates: list[str] = []
    other_candidates: list[str] = []
    for m in _LINK_RE.finditer(resp.text):
        attrs = _parse_attrs(m.group(1))
        rel = attrs.get("rel", "").lower()
        mtype = attrs.get("type", "").split(";")[0].strip().lower()
        href = attrs.get("href", "").strip()
        if "alternate" in rel and mtype in _FEED_MIME_TYPES and href:
            absolute = urljoin(final_url, href)
            bucket = atom_candidates if mtype == "application/atom+xml" else other_candidates
            if absolute not in atom_candidates and absolute not in other_candidates:
                bucket.append(absolute)
    candidates = atom_candidates + other_candidates

    if candidates:
        return candidates

    # Probe common path suffixes with HEAD requests.
    parsed = urlparse(final_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    for suffix in _COMMON_FEED_PATHS:
        probe_url = origin + suffix
        try:
            head = httpx.head(probe_url, timeout=5.0, follow_redirects=True, headers=_HEADERS)
            if head.is_success and _ct_is_feed(head.headers.get("content-type", "")):
                resolved = str(head.url)
                if resolved not in candidates:
                    candidates.append(resolved)
                break  # first working path is enough
        except Exception:
            continue

    return candidates
