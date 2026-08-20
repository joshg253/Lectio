"""Repair image URLs that were resolved against the FEED's directory.

Until the ingest fix, feedparser resolved an item's relative `<img src>` against
the document base, which reader sets to the feed URL. A generator that copies a
page's markup into the item writes those paths relative to THAT PAGE, so an
image in an item at /news/202608/post.html was stored as /news/images/x.jpg and
404s. New entries are handled at ingest (services/reader_sanitize); this is the
one-off for entries already stored.

Self-verifying, because the signature alone cannot tell a mis-resolved path from
a root-relative one that never depended on the base: a URL is only rewritten
when the STORED url fails and the item-relative one returns 200. Nothing is
guessed.

Usage (inside the app container):
    /app/.venv/bin/python scripts/repair_relative_image_urls.py            # dry-run
    /app/.venv/bin/python scripts/repair_relative_image_urls.py --apply
    /app/.venv/bin/python scripts/repair_relative_image_urls.py --apply --user u_x
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from urllib.parse import urljoin, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

import main  # noqa: E402
from services import refetch_batch, tenancy  # noqa: E402

_IMG_SRC = re.compile(r'(<img\b[^>]*?\bsrc=")([^"]+)(")', re.IGNORECASE)
_UA = {"User-Agent": "Lectio/1.0 (+feed reader; one-off broken-image repair)"}
_GLOBAL_DELAY = refetch_batch.GLOBAL_DELAY
_PER_HOST_DELAY = refetch_batch.PER_HOST_DELAY


class _Prober:
    """HEAD (falling back to GET) with the same pacing the re-fetch batch uses."""

    _CACHE_CAP = 5000                 # a run probes one feed's images; this is slack

    def __init__(self) -> None:
        self._cache: dict[str, int | str] = {}
        self._last_global = 0.0
        self._last_host: dict[str, float] = {}

    def _wait(self, host: str) -> None:
        now = time.monotonic()
        for ready in (self._last_global + _GLOBAL_DELAY, self._last_host.get(host, 0.0) + _PER_HOST_DELAY):
            if ready > now:
                time.sleep(ready - now)
                now = time.monotonic()
        self._last_global = now
        self._last_host[host] = now

    def status(self, url: str) -> int | str:
        if url in self._cache:
            return self._cache[url]
        host = urlparse(url).netloc
        self._wait(host)
        try:
            resp = httpx.head(url, headers=_UA, follow_redirects=True, timeout=15)
            if resp.status_code in (403, 405, 501):  # HEAD refused, not the image's fault
                resp = httpx.get(url, headers=_UA, follow_redirects=True, timeout=15)
            out: int | str = resp.status_code
        except Exception as exc:  # noqa: BLE001 — a dead host is a result, not a crash
            out = type(exc).__name__
        if len(self._cache) >= self._CACHE_CAP:
            self._cache.clear()       # bounded: a repeat probe costs one request
        self._cache[url] = out
        return out


def _suspects(summary: str, feed_dir: str, entry_dir: str) -> list[str]:
    """Stored img URLs that sit under the feed's directory but not the item's."""
    return [u for u in (m.group(2) for m in _IMG_SRC.finditer(summary or ""))
            if u.startswith(feed_dir) and not u.startswith(entry_dir)]


def run(uid: str, apply: bool, only_feed: str | None) -> None:
    with main.get_reader() as reader:
        db = reader._storage.get_db()
        rows = db.execute(
            "SELECT feed, id, link, summary, content FROM entries"
            " WHERE summary LIKE '%<img%' AND link IS NOT NULL AND link != ''"
        ).fetchall()

    candidates: list[tuple] = []
    for feed, entry_id, link, summary, content in rows:
        if only_feed and feed != only_feed:
            continue
        feed_dir, entry_dir = urljoin(feed, "."), urljoin(link, ".")
        if entry_dir == feed_dir:
            continue                     # same directory: the base never mattered
        hits = _suspects(summary, feed_dir, entry_dir)
        if hits:
            candidates.append((feed, entry_id, link, summary, content, feed_dir, entry_dir, hits))

    if not candidates:
        print(f"[{uid}] nothing to check")
        return
    hosts = {urlparse(f).netloc for f, *_ in candidates}
    print(f"[{uid}] {len(candidates)} candidate entries across {len(hosts)} host(s); probing…")

    prober = _Prober()
    writes: list[tuple] = []          # applied once at the end, on one connection
    fixed: Counter = Counter()
    skipped_ok: Counter = Counter()
    dead: Counter = Counter()
    for feed, entry_id, link, summary, content, feed_dir, _entry_dir, hits in candidates:
        rewrites: dict[str, str] = {}
        for stored in dict.fromkeys(hits):
            repaired = urljoin(link, stored[len(feed_dir):])
            if repaired == stored:
                continue
            if prober.status(stored) == 200:
                skipped_ok[feed] += 1     # stored URL is fine — root-relative original
                continue
            if prober.status(repaired) == 200:
                rewrites[stored] = repaired
            else:
                dead[feed] += 1           # gone from the server either way
        if not rewrites:
            continue
        new_summary = _IMG_SRC.sub(
            lambda m, _r=rewrites: m.group(1) + _r.get(m.group(2), m.group(2)) + m.group(3), summary)
        new_content = content
        if content:
            for old, new in rewrites.items():
                new_content = new_content.replace(json.dumps(old)[1:-1], json.dumps(new)[1:-1])
        fixed[feed] += len(rewrites)
        print(f"   {'fix ' if apply else 'would fix '}{len(rewrites)} img · {entry_id[:88]}")
        if apply:
            writes.append((new_summary, new_content, feed, entry_id))

    if writes:
        # One reader/DB handle for the whole run: reopening per entry meant a
        # connection setup and a commit for each one.
        with main.get_reader() as reader:
            db = reader._storage.get_db()
            db.executemany(
                "UPDATE entries SET summary = ?, content = ? WHERE feed = ? AND id = ?", writes)
            db.commit()

    print(f"\n[{uid}] {'repaired' if apply else 'would repair'}: "
          f"{sum(fixed.values())} images in {len(fixed)} feed(s)")
    for feed, n in fixed.most_common():
        print(f"     {n:>4}  {urlparse(feed).netloc}")
    print(f"[{uid}] left alone: {sum(skipped_ok.values())} already-working, "
          f"{sum(dead.values())} missing from the server")


def main_cli() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write the repairs (default: dry-run)")
    ap.add_argument("--user", default=None, help="restrict to one user_id")
    ap.add_argument("--feed", default=None, help="restrict to one feed URL")
    args = ap.parse_args()
    for uid in ([args.user] if args.user else main._background_user_ids()):
        with tenancy.user_context(uid):
            run(uid, args.apply, args.feed)
    if not args.apply:
        print("\ndry-run — nothing written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
