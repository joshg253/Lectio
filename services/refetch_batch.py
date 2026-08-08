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
import random
import time
from collections import defaultdict
from collections.abc import Callable
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


def run_paced(
    rows: list[tuple[str, str, str]],
    refetch: Callable[[str, str], dict],
    *,
    on_progress: Callable[[int, int, dict[str, int]], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[], float] = random.random,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[dict[str, int], list[dict]]:
    """Re-fetch every row, obeying the pacing above. Serial, by design.

    *rows* are ``(feed_url, entry_id, link)`` and are taken in the order given —
    interleave them with :func:`interleave_by_host` first. *refetch* is the
    per-entry call (in practice ``_refresh_captured_article_for_current_user``);
    every guard, snapshot and archive fallback it carries applies per entry,
    which is what makes running this over hundreds of articles reasonable.

    Returns ``(stats, log)``. Callers report and persist those; the outcome
    vocabulary is fixed here so two callers cannot count the same result under
    different names. ``mismatch`` covers a refusal — the page was a different
    article, or the extraction was the feed's boilerplate again — and a refusal
    is a success for the stored copy, which is left exactly as it was.

    The clock and randomness are injected so a test can run this instantly
    without either mocking out the pacing (which would stop testing it) or
    actually waiting ten seconds a host.
    """
    stats = {"ok": 0, "archive": 0, "mismatch": 0, "dead": 0, "failed": 0, "skipped_host": 0}
    log: list[dict] = []
    host_failures: dict[str, int] = defaultdict(int)
    host_last: dict[str, float] = {}

    for i, (feed_url, entry_id, link) in enumerate(rows, 1):
        host = host_of(link)
        if host_failures[host] >= HOST_FAILURE_LIMIT:
            stats["skipped_host"] += 1
            continue
        wait = PER_HOST_DELAY - (monotonic() - host_last.get(host, 0.0))
        if wait > 0:
            sleep(wait)
        sleep(GLOBAL_DELAY * (0.5 + jitter()))
        host_last[host] = monotonic()

        result = refetch(feed_url, entry_id)
        if result.get("ok"):
            stats["archive" if result.get("from_archive") else "ok"] += 1
            host_failures[host] = 0
        elif result.get("mismatch"):
            stats["mismatch"] += 1          # stored copy deliberately left alone
            host_failures[host] = 0
        elif result.get("dead"):
            stats["dead"] += 1
            host_failures[host] = 0
        else:
            stats["failed"] += 1
            host_failures[host] += 1
        log.append({"feed_url": feed_url, "entry_id": entry_id, "link": link,
                    "ok": bool(result.get("ok")), "error": result.get("error"),
                    "from_archive": result.get("from_archive"),
                    "dated": result.get("dated")})
        if on_progress and i % 10 == 0:
            on_progress(i, len(rows), dict(stats))

    return stats, log
