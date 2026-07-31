"""Fetch the pages of epoch-dated entries just to learn their publish date.

The offline recovery pass (`recover_publish_dates.py`) took 2,030 of 3,308 entries
from already-captured HTML. The rest have no stored page, or a stored page with no
date in it — the only way they get a date is asking the site.

**Date only.** Nothing about the article is replaced: not the content, not the
title, not the archive. A re-fetch that replaces content is how the-digital-reader
entry was destroyed (a parked page returning 200), and this pass must not be able to
repeat that no matter what a site now serves.

Guards, in the order they matter:

- **The slug check.** If the fetched page's <title> shares no significant word with
  the URL's own slug, the page is something else and its date is not ours. Same
  test the interactive re-fetch uses.
- **The save-date bound.** You cannot have saved an article before it was
  published, so a candidate later than the Instapaper save timestamp is rejected.
- **Range.** 1900 is a template placeholder; a future date is a clock problem.
- **Never overrides** an `entry_date_overrides` row, and only ever writes over the
  epoch.

**Politeness** is the reason this is a script and not a button. Requests are paced
globally and per host, hosts are dropped after repeated failures (these are largely
2019-era saves, so dead domains are common), and the honest Lectio user-agent is
used throughout — no browser impersonation, no retry storm.

⚠ **MEASURED 2026-07-30: not worth running as things stand.** 253 epoch-dated
entries remain (down from 1,278 — feed refreshes re-ingested most of the rest with
real dates), and they cluster on a handful of hosts that publish no date metadata
at all: blog.guitar-pro.com (96), joanwestenberg.com (45), what-if.xkcd.com (45),
datagenetics.com (27). A 25-entry sample returned **zero** dates, and probing one
page per host across 8 hosts also returned zero. One of those answered 404 with the
404 page's own date — the exact wrong answer the guards reject.

Kept because it is written, safe and cheap to re-run: if a batch of new epoch-dated
entries arrives from a site that *does* publish metadata, this is the tool. Sample
with --limit before committing to a full pass.

    uv run python scripts/fetch_missing_publish_dates.py --limit 25     # sample
    uv run python scripts/fetch_missing_publish_dates.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from services import tenancy, url_guard  # noqa: E402
from services.saved_articles import _page_is_a_different_article  # noqa: E402

EPOCH_PREFIX = "1970-01-01"
_GLOBAL_DELAY = 1.0        # seconds between requests, whatever the host
_PER_HOST_DELAY = 5.0      # and at least this long between two hits on one host
_HOST_FAILURE_LIMIT = 5    # drop a host after this many consecutive failures
_TIMEOUT = 12.0
_SAVE_SLACK = timedelta(days=1)


def _page_title(raw_html: str) -> str:
    import re
    m = re.search(r"<title[^>]*>(.*?)</title>", raw_html or "", re.I | re.S)
    return " ".join((m.group(1) if m else "").split())[:200]


def run_for_user(uid: str, apply: bool, limit: int | None) -> dict:
    meta = sqlite3.connect(f"file:{tenancy.meta_db_path()}?mode=ro", uri=True, timeout=30.0)
    meta.row_factory = sqlite3.Row
    saved_at = {}
    for row in meta.execute("SELECT feed_url, entry_id, saved_at FROM saved_entries"):
        dt = main._parse_stored_dt(row["saved_at"])
        if dt is not None:
            saved_at[(str(row["feed_url"]), str(row["entry_id"]))] = (
                dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))
    overrides = {
        (str(r["feed_url"]), str(r["entry_id"]))
        for r in meta.execute("SELECT feed_url, entry_id FROM entry_date_overrides")
    }
    meta.close()

    rc = sqlite3.connect(str(tenancy.reader_db_path()), timeout=30.0)
    rc.row_factory = sqlite3.Row
    rows = [
        r for r in rc.execute(
            "SELECT feed, id, link, title FROM entries WHERE published LIKE ?",
            (EPOCH_PREFIX + "%",),
        ).fetchall()
        if (str(r["feed"]), str(r["id"])) not in overrides
    ]
    # Interleave hosts so one slow site doesn't hold up the run, and so no host
    # sees a burst even before the per-host delay applies.
    by_host: dict[str, list] = defaultdict(list)
    for r in rows:
        link = str(r["link"] or r["id"] or "")
        host = (urlparse(link).netloc or "").lower()
        if link.startswith(("http://", "https://")) and host:
            by_host[host].append(r)
    ordered: list = []
    while by_host:
        for host in list(by_host):
            ordered.append(by_host[host].pop(0))
            if not by_host[host]:
                del by_host[host]
    if limit:
        ordered = ordered[:limit]

    stats = {"tried": 0, "dated": 0, "no_date": 0, "mismatch": 0, "failed": 0, "skipped_host": 0}
    found: list[dict] = []
    host_failures: dict[str, int] = defaultdict(int)
    host_last: dict[str, float] = {}

    print(f"[{uid}] {len(rows):,} epoch-dated with a URL; attempting {len(ordered):,}")
    headers = {"User-Agent": main.READABILITY_USER_AGENT}

    for r in ordered:
        key = (str(r["feed"]), str(r["id"]))
        link = str(r["link"] or r["id"])
        host = (urlparse(link).netloc or "").lower()
        if host_failures[host] >= _HOST_FAILURE_LIMIT:
            stats["skipped_host"] += 1
            continue

        wait = _PER_HOST_DELAY - (time.monotonic() - host_last.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        time.sleep(_GLOBAL_DELAY * (0.5 + random.random()))
        host_last[host] = time.monotonic()
        stats["tried"] += 1

        try:
            with url_guard.build_client(timeout=_TIMEOUT, headers=headers) as client:
                resp = url_guard.safe_get(client, link, headers=headers)
            resp.raise_for_status()
            raw = resp.text
        except Exception:  # noqa: BLE001 — dead domains are the norm here
            host_failures[host] += 1
            stats["failed"] += 1
            continue
        host_failures[host] = 0

        if _page_is_a_different_article(link, _page_title(raw)):
            stats["mismatch"] += 1
            continue
        mined = main.mine_publish_date(raw)
        if mined is None:
            stats["no_date"] += 1
            continue
        bound = saved_at.get(key)
        if bound is not None and mined > bound + _SAVE_SLACK:
            stats["mismatch"] += 1
            continue

        stored = mined.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        found.append({"feed_url": key[0], "entry_id": key[1],
                      "title": str(r["title"] or "")[:70], "published": stored})
        stats["dated"] += 1
        if apply:
            rc.execute("UPDATE entries SET published = ? WHERE feed = ? AND id = ?",
                       (stored, key[0], key[1]))
            if len(found) % 25 == 0:
                rc.commit()
                print(f"   … {stats['dated']:,} dated, {stats['tried']:,} tried")

    if apply:
        rc.commit()
        out = tenancy.meta_db_path().parent / f"fetched_publish_dates_{datetime.now():%Y%m%d-%H%M%S}.json"
        out.write_text(json.dumps(found, indent=2))
        print(f"   log: {out}")
    rc.close()
    print(f"[{uid}] tried {stats['tried']:,} | dated {stats['dated']:,} | "
          f"no date {stats['no_date']:,} | wrong page {stats['mismatch']:,} | "
          f"failed {stats['failed']:,} | skipped (dead host) {stats['skipped_host']:,}")
    if not apply:
        print("   dry run — re-run with --apply to write")
    return stats


def main_cli() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write dates (default: dry run)")
    ap.add_argument("--limit", type=int, default=None, help="stop after N fetches")
    ap.add_argument("--user", default=None, help="restrict to one user_id")
    args = ap.parse_args()

    for uid in ([args.user] if args.user else main._background_user_ids()):
        with tenancy.user_context(uid):
            run_for_user(uid, args.apply, args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
