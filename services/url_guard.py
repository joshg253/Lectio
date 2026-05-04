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
    for info in infos:
        sockaddr = info[4]
        # sockaddr is (host, port) for AF_INET or (host, port, flowinfo, scopeid)
        # for AF_INET6 — host is always a str.
        ip_str = str(sockaddr[0])
        if _is_private_or_local(ip_str):
            return False
    return True
