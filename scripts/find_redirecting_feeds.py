"""Find subscribed feeds reached through an HTTP redirect and report where the
final URL differs.

Read-only by default: it reports, it does not change subscriptions. A feed
reached through a 301 costs two requests per poll forever, and it dies
silently the day the publisher retires the redirect (which they do once a
migration finishes) -- see Plan.md "Redirecting feeds — no way to find them
in bulk".

Two distinctions this makes, or reporting/applying it does damage:

  1. **301 vs 302.** A temporary redirect must not be reported as a candidate
     -- it may well go back to answering directly tomorrow.
  2. **Moved vs replaced.** A hop that lands on a genuinely different feed (a
     site-wide firehose, a FeedBurner default) is not the same feed at a new
     address -- the same "a discovered feed is not a replacement" trap
     probe_dead_feeds.py already guards against, reused here via
     _looks_like_same_feed.

--apply calls main.change_feed_url_route directly (same function the Change
Feed URL UI posts to, and the same pattern scripts/apply_feed_url_rewrites.py
already uses for a live-app helper) with force=0, so its own independent
probe_url verification and full meta-table migration still run -- this script
only picks which URLs are worth offering it, never bypasses its checks.

Politeness (see the good-web-citizen rule): one request at a time with a delay
between feeds, the honest Lectio UA, follow_redirects so intermediate hop
codes are visible in response.history.

Usage:
    LECTIO_DATA_DIR=/data uv run scripts/find_redirecting_feeds.py --user <id> \\
        [--limit N] [--delay 1.0] [--json out.json] [--apply]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from urllib.parse import urlparse

sys.path.insert(0, "/app")

import feedparser  # noqa: E402
import httpx  # noqa: E402

UA = "Lectio/0.1 (+https://github.com/joshg253/Lectio)"
TIMEOUT = 15.0


def _norm_title(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _looks_like_same_feed(parsed, original_url: str, stored_title: str) -> tuple[bool, str]:
    """Mirrors probe_dead_feeds.py's check: same host in the entry links, or a
    recognizably similar feed title. A redirect target that fails both looks
    like a site-wide firehose standing in for what used to be a scoped feed."""
    if not parsed.entries:
        return False, "parsed but has no entries"
    host = (urlparse(original_url).hostname or "").lower().removeprefix("www.")
    for e in parsed.entries[:10]:
        link = getattr(e, "link", "") or ""
        h = (urlparse(link).hostname or "").lower().removeprefix("www.")
        if h and (h == host or h.endswith("." + host) or host.endswith("." + h)):
            return True, "entry links match the original host"
    new_title = _norm_title(getattr(parsed.feed, "title", ""))
    old_title = _norm_title(stored_title)
    if new_title and old_title and (new_title in old_title or old_title in new_title):
        return True, "feed title matches the subscription"
    return False, f"entries point elsewhere (title={getattr(parsed.feed, 'title', '')!r})"


def probe_one(client: httpx.Client, url: str, stored_title: str) -> dict:
    out = {"url": url, "title": stored_title, "final_url": None, "hops": [],
           "verdict": "direct", "reason": None}
    try:
        r = client.get(url)
    except Exception as exc:  # noqa: BLE001
        out["verdict"] = "error"
        out["reason"] = f"{type(exc).__name__}"
        return out

    out["hops"] = [h.status_code for h in r.history]
    out["final_url"] = str(r.url)
    if not r.history or str(r.url) == url:
        out["verdict"] = "direct"
        return out

    if any(code != 301 for code in out["hops"]):
        out["verdict"] = "temporary"
        out["reason"] = "at least one hop was not a 301 — may revert on its own"
        return out

    if r.status_code != 200 or not r.content:
        out["verdict"] = "redirects-to-dead"
        out["reason"] = f"final hop returned HTTP {r.status_code}"
        return out

    parsed = feedparser.parse(r.content)
    if not getattr(parsed, "feed", None):
        out["verdict"] = "redirects-to-non-feed"
        out["reason"] = "final URL doesn't parse as a feed"
        return out

    ok, why = _looks_like_same_feed(parsed, url, stored_title)
    out["reason"] = why
    out["verdict"] = "candidate" if ok else "redirects-elsewhere"
    return out


def main_cli(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--json")
    ap.add_argument("--apply", action="store_true", help="Apply every 'candidate' verdict via Change Feed URL.")
    args = ap.parse_args(argv)

    base = f"/data/users/{args.user}"
    r = sqlite3.connect(f"file:{base}/lectio_reader.sqlite?mode=ro", uri=True)
    targets = [(u, t or "") for u, t in r.execute("SELECT url, title FROM feeds WHERE url LIKE 'http%'")]
    if args.limit:
        targets = targets[:args.limit]
    print(f"probing {len(targets)} feeds for redirects\n", flush=True)

    results = []
    with httpx.Client(headers={"User-Agent": UA}, timeout=TIMEOUT, follow_redirects=True) as client:
        for i, (url, title) in enumerate(targets, 1):
            res = probe_one(client, url, title)
            results.append(res)
            if res["verdict"] != "direct":
                mark = {"candidate": "CANDIDATE", "temporary": "temp    ", "redirects-elsewhere": "elsewhere",
                        "redirects-to-dead": "dead    ", "redirects-to-non-feed": "non-feed",
                        "error": "error   "}.get(res["verdict"], res["verdict"])
                print(f"  [{i:>4}/{len(targets)}] {mark}  {url[:60]}", flush=True)
                if res["final_url"] and res["final_url"] != url:
                    print(f"               -> {res['final_url'][:70]}  ({res['reason']})", flush=True)
            time.sleep(args.delay)

    n = {k: sum(1 for x in results if x["verdict"] == k)
         for k in ("direct", "candidate", "temporary", "redirects-elsewhere", "redirects-to-dead",
                    "redirects-to-non-feed", "error")}
    print(f"\ndirect: {n['direct']}   candidates (301, verified same feed): {n['candidate']}   "
          f"temporary (302 etc): {n['temporary']}   redirects elsewhere: {n['redirects-elsewhere']}   "
          f"redirects to dead/non-feed: {n['redirects-to-dead'] + n['redirects-to-non-feed']}   "
          f"errors: {n['error']}")
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(results, fh, indent=2)
        print(f"wrote {args.json}")

    if args.apply:
        import main  # noqa: E402
        from services import tenancy  # noqa: E402
        candidates = [x for x in results if x["verdict"] == "candidate"]
        print(f"\napplying {len(candidates)} candidate(s) via change_feed_url_route (force=0)...")
        applied = skipped = 0
        with tenancy.user_context(args.user):
            for x in candidates:
                resp = main.change_feed_url_route(old_url=x["url"], new_url=x["final_url"], force=0)
                ok = getattr(resp, "status_code", 500) < 400
                print(f"  {'OK ' if ok else 'ERR'}  {x['url'][:60]} -> {x['final_url'][:60]}")
                if ok:
                    applied += 1
                else:
                    skipped += 1
        print(f"applied: {applied}   skipped/failed: {skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
