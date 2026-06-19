"""SSRF guardrail for outbound URL fetches.

Lectio fetches URLs that originate from external feed content (entry links,
og:image targets, source-page scraping). On a VPS with private-network access
(Docker bridges, Tailscale, internal services), an attacker who lands one
malicious feed entry into the user's subscriptions could probe internal
targets via Lectio's HTTP client. This module rejects URLs that resolve to
private/loopback/link-local IP space before the fetch happens.

Usage:
    from services.url_guard import is_safe_outbound_url

    if not is_safe_outbound_url(entry_link):
        return None
    response = httpx.get(entry_link, ...)

Bypass: when LECTIO_DEBUG=1, all checks pass so LAN test feeds and local dev
servers (192.168.x.x, 127.0.0.1) work normally.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse

import httpx

# Max redirect hops to follow when fetching an externally-influenced URL. Each
# hop is re-validated, so this just bounds work / loops.
DEFAULT_MAX_REDIRECTS = 5


class UnsafeURLError(Exception):
    """Raised when a fetch target (initial URL or a redirect hop) is unsafe."""


def _debug_bypass_enabled() -> bool:
    return os.getenv("LECTIO_DEBUG", "0") == "1"


def _is_private_or_local(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        # Unparseable address — refuse to fetch.
        return True
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def is_safe_outbound_url(url: str) -> bool:
    """Return True if the URL is safe to fetch from a public-internet host.

    Returns False when:
      - URL is malformed or non-http(s)
      - Hostname is missing
      - Hostname resolves to any private / loopback / link-local IP

    Returns True (bypassing all checks) when LECTIO_DEBUG=1 so dev workflows
    pointing at LAN feeds aren't blocked.
    """
    if _debug_bypass_enabled():
        return True

    if not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False

    # If the hostname is a literal IP, check it directly without going through DNS.
    try:
        ipaddress.ip_address(host)
        is_literal_ip = True
    except ValueError:
        is_literal_ip = False
    if is_literal_ip and _is_private_or_local(host):
        return False

    # Resolve the hostname to all addresses; refuse if any are private. (DNS
    # rebinding mitigation: if any returned address is internal, treat the
    # whole URL as unsafe even if other addresses are public.)
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        # DNS lookup failed; refuse rather than fetch into an unknown target.
        return False
    has_public = False
    for info in infos:
        sockaddr = info[4]
        # sockaddr is (host, port) for AF_INET or (host, port, flowinfo, scopeid)
        # for AF_INET6 — host is always a str.
        ip_str = str(sockaddr[0])
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        # Link-local IPv6 (fe80::/10) addresses are interface artifacts returned
        # by getaddrinfo on some container networking configurations. They are not
        # actual server addresses, so exclude them from the rebinding check.
        if ip.is_link_local:
            continue
        if _is_private_or_local(ip_str):
            return False
        has_public = True
    # Require at least one confirmed public address (guards against all-private results).
    return has_public


def _redirect_target(resp: httpx.Response) -> str | None:
    """Absolute URL of a redirect response's Location, or None."""
    loc = resp.headers.get("location")
    if not loc:
        return None
    # Resolve relative redirects against the responding request's URL.
    return str(resp.url.join(loc))


def safe_get(
    client: httpx.Client,
    url: str,
    *,
    headers: dict | None = None,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
) -> httpx.Response:
    """SSRF-safe sync GET that validates the initial URL AND every redirect hop.

    ``client`` MUST be created with ``follow_redirects=False`` so this function
    controls redirect handling. Each hop is checked with
    :func:`is_safe_outbound_url` before the request is made, closing the gap
    where httpx's automatic redirect following would bounce a public URL to an
    internal address after the pre-check passed. Raises :class:`UnsafeURLError`
    for an unsafe hop or too many redirects.
    """
    current = url
    for _ in range(max_redirects + 1):
        if not is_safe_outbound_url(current):
            raise UnsafeURLError(current)
        resp = client.get(current, headers=headers)
        if resp.is_redirect:
            nxt = _redirect_target(resp)
            if nxt is None:
                return resp  # 3xx without Location — hand back as-is
            current = nxt
            continue
        return resp
    raise UnsafeURLError(f"too many redirects starting from {url!r}")


def safe_head(
    url: str,
    *,
    timeout: float = 5.0,
    headers: dict | None = None,
) -> httpx.Response:
    """SSRF-safe HEAD probe with its own ``follow_redirects=False`` client.

    HEAD has no per-hop revalidation counterpart to :func:`safe_get` (a redirect
    just comes back as a 3xx), so this validates the target URL up front and
    refuses to follow redirects, closing the redirect-to-internal bypass for the
    image-fetchability / comic-URL probes. Raises :class:`UnsafeURLError` for an
    unsafe target.
    """
    if not is_safe_outbound_url(url):
        raise UnsafeURLError(url)
    with httpx.Client(follow_redirects=False, timeout=timeout, headers=headers) as client:
        return client.head(url)


async def safe_get_async(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict | None = None,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
) -> httpx.Response:
    """Async counterpart of :func:`safe_get`. ``client`` MUST use
    ``follow_redirects=False``."""
    current = url
    for _ in range(max_redirects + 1):
        if not is_safe_outbound_url(current):
            raise UnsafeURLError(current)
        resp = await client.get(current, headers=headers)
        if resp.is_redirect:
            nxt = _redirect_target(resp)
            if nxt is None:
                return resp
            current = nxt
            continue
        return resp
    raise UnsafeURLError(f"too many redirects starting from {url!r}")
