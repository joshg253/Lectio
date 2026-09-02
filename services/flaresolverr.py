"""FlareSolverr /v1 wire protocol — request body construction and response
envelope parsing, shared by the feed-refresh escalation ladder
(services/reader_api.py) and the page-fetch escalation ladder
(services/page_fetch.py).

Deliberately does not know about requests/httpx, sessions, or hooks — this
module only speaks the JSON contract at FlareSolverr's HTTP boundary.
"""

from __future__ import annotations

import html as _html
import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from services import bot_challenge, url_guard

# FlareSolverr's /v1 endpoint doesn't return the origin's raw bytes — it
# returns real Chrome's rendered `outerHTML`. For a feed (always served as
# XML) that's Chrome's own "view source" wrapper: the entire document sits
# HTML-entity-escaped inside one <pre>. Verified empirically against a real
# Cloudflare-protected feed (cpp.libhunt.com) — not a documented FlareSolverr
# contract, just what Chrome always does with an XML response it can't render.
_FLARESOLVERR_PRE_RE = re.compile(rb"<pre[^>]*>(.*)</pre>", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class Solution:
    """A parsed FlareSolverr /v1 'solution' envelope. ``html`` is Chrome's
    rendered outerHTML as-is — a feed caller still needs unwrap_view_source
    on top of it; a page caller can use it directly."""

    html: str
    status: int | None
    url: str


def build_request_body(url: str, *, proxy_url: str | None = None, max_timeout_ms: int = 55_000) -> dict[str, object]:
    """FlareSolverr /v1 request body for a single-shot 'request.get' solve."""
    body: dict[str, object] = {"cmd": "request.get", "url": url, "maxTimeout": max_timeout_ms}
    if proxy_url:
        body["proxy"] = {"url": proxy_url}
    return body


def parse_envelope(payload: dict, request_url: str) -> Solution:
    """Parse a decoded FlareSolverr /v1 JSON response into a Solution.

    Raises bot_challenge.FeedBlockedError if FlareSolverr itself gave up (its
    own timeout, browser crash, etc) rather than returning a page — framed as
    a challenge failure since that's what sent the fetch here in the first
    place. Does NOT unwrap Chrome's XML view-source <pre> wrapper — a feed
    caller does that separately via unwrap_view_source; a page caller uses
    ``solution.html`` as real HTML directly.
    """
    solution = payload.get("solution") or {}
    if payload.get("status") != "ok" or not solution:
        raise bot_challenge.FeedBlockedError(
            f"FlareSolverr: {payload.get('message') or 'no solution returned'}",
            request_url,
        )
    origin_status = solution.get("status")
    return Solution(
        html=str(solution.get("response") or ""),
        status=origin_status if isinstance(origin_status, int) else None,
        url=str(solution.get("url") or request_url),
    )


def unwrap_view_source(html_content: str) -> bytes | None:
    """Extract a feed's original bytes from Chrome's XML view-source <pre>
    wrapper, or None if html_content isn't that wrapper (real HTML, a
    block/login page, etc — feed-only; a page fetch never needs this)."""
    pre_match = _FLARESOLVERR_PRE_RE.search(html_content.encode("utf-8", errors="replace"))
    if not pre_match:
        return None
    return _html.unescape(pre_match.group(1).decode("utf-8", errors="replace")).encode("utf-8")


def solve(
    endpoint: str,
    target_url: str,
    *,
    proxy_url: str | None = None,
    timeout: float = 60.0,
    max_timeout_ms: int = 55_000,
) -> Solution:
    """POST to the admin-configured FlareSolverr endpoint and return its parsed Solution.

    Used by the page-fetch escalation ladder (services/page_fetch.py) — the
    feed-refresh ladder reaches FlareSolverr a different way, via a reader
    request hook (services/reader_api.py), not through this function.

    SSRF boundary: ``target_url`` is externally influenced (a feed's article
    link) and MUST be safe to fetch — checked here, fail-closed, same as every
    other outbound fetch in this codebase. ``endpoint`` is admin-only instance
    configuration (the flaresolverr_url setting), never request input — the
    same trust basis on which reader's own FlareSolverr hook bypasses
    url_guard for this exact call (see docs/architecture/feeds.md). It is
    deliberately NOT run through is_safe_outbound_url, which would refuse
    every real deployment (a Docker-internal RFC1918 address like
    http://flaresolverr:8191/v1).
    """
    if not url_guard.is_safe_outbound_url(target_url):
        raise url_guard.UnsafeURLError(target_url)
    if urlparse(endpoint).scheme not in ("http", "https"):
        raise ValueError(f"FlareSolverr endpoint must be http(s): {endpoint!r}")
    body = build_request_body(target_url, proxy_url=proxy_url, max_timeout_ms=max_timeout_ms)
    # verify=WEB_SSL_CONTEXT so a compromised/misconfigured solver can't be used
    # to bounce this POST elsewhere; plain client.post (not url_guard.safe_post)
    # since endpoint is trusted config, not the guarded target — deliberate.
    with httpx.Client(follow_redirects=False, verify=url_guard.WEB_SSL_CONTEXT, timeout=timeout) as client:
        response = client.post(endpoint, headers={"Content-Type": "application/json"}, content=json.dumps(body).encode("utf-8"))
    response.raise_for_status()
    return parse_envelope(response.json(), target_url)


def normalize_proxy_scheme(proxy_url: str) -> str:
    """FlareSolverr passes this straight to Chrome's --proxy-server flag,
    which only understands plain socks5:// — not socks5h://. The primary
    proxy's OWN configured URL is socks5h:// on purpose (pysocks/requests
    needs the "h" to do DNS resolution through the proxy rather than
    locally), so the two consumers of the same setting need different
    spellings. Confirmed empirically (2026-08-30): socks5h:// silently broke
    Chrome's proxy config and it fell through to a bare connection error
    page instead of the real site."""
    if proxy_url.startswith("socks5h://"):
        return "socks5://" + proxy_url[len("socks5h://"):]
    return proxy_url
