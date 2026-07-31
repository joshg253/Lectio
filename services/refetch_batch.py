"""Shared plumbing for re-fetching many articles slowly.

Used by both the background job behind the UI action and
``scripts/refetch_scope.py``, so the pacing cannot drift between them — a "be
gentle" guarantee that only holds in one of two entry points is not a guarantee.

Pacing is the design, not a setting: a global gap between requests, a much longer
one per host, hosts dropped after repeated failures, hosts interleaved so no site
sees a run of back-to-back hits, and nothing parallel.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from urllib.parse import urlparse

LOGGER = logging.getLogger(__name__)

GLOBAL_DELAY = 2.0        # seconds between requests, whatever the host
PER_HOST_DELAY = 10.0     # and at least this long between two hits on one host
HOST_FAILURE_LIMIT = 4    # drop a host after this many consecutive failures


def host_of(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower()
    except ValueError:
        return ""


def interleave_by_host(rows: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """Round-robin the work across hosts.

    Two reasons: one slow site cannot hold up everything behind it, and no host
    sees a burst even before the per-host delay starts applying.
    """
    by_host: dict[str, list] = defaultdict(list)
    for row in rows:
        by_host[host_of(row[2])].append(row)
    out: list[tuple[str, str, str]] = []
    while by_host:
        for host in list(by_host):
            out.append(by_host[host].pop(0))
            if not by_host[host]:
                del by_host[host]
    return out


def estimate_seconds(rows: list[tuple[str, str, str]]) -> float:
    """How long the run will actually take.

    Must account for the PER-HOST delay, not just the global one: a single-feed
    scope is one host, so 89 articles is 89x10s rather than 89x2s. Understating the
    runtime of a deliberately slow job is the one number that must not be wrong.
    """
    if not rows:
        return 0.0
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[host_of(row[2])] += 1
    busiest = max(counts.values())
    return max(len(rows) * GLOBAL_DELAY * 1.5, busiest * PER_HOST_DELAY)
