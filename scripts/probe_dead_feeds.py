"""Probe feeds failing with 404 and look for a replacement URL.

Read-only by default: it reports, it does not change subscriptions.

Why a replacement must be *verified*, not just discovered: a site that dropped
its feed usually still returns 200 for its homepage, and autodiscovery on that
page happily hands back a link. Subscribing to that would swap a broken feed for
a wrong one, which is worse — it looks fixed. So a candidate is only accepted if

  1. it actually parses as a feed (feedparser finds a title and entries), and
  2. it looks like the SAME feed — entry links on the original host, or a title
     resembling the subscription's stored title.

Politeness (see the good-web-citizen rule): one request at a time with a delay
between hosts, the honest Lectio UA, and per-domain failures give up rather than
retry. These are already-dead URLs; there is no hurry.

Usage:
    LECTIO_DATA_DIR=/data uv run scripts/probe_dead_feeds.py --user <id> \\
        [--limit N] [--delay 1.5] [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from urllib.parse import urljoin, urlparse

sys.path.insert(0, "/app")

import feedparser  # noqa: E402
import httpx  # noqa: E402

UA = "Lectio/0.1 (+https://github.com/joshg253/Lectio)"
TIMEOUT = 15.0


def _norm_title(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _looks_like_same_feed(parsed, original_url: str, stored_title: str) -> tuple[bool, str]:
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
    out = {"url": url, "title": stored_title, "status": None,
           "candidate": None, "reason": None, "verdict": "dead"}
    try:
        r = client.get(url)
        out["status"] = r.status_code
        if r.status_code == 200 and "html" not in (r.headers.get("content-type", "").lower()):
            out["verdict"] = "recovered"
            out["reason"] = "the original URL responds again"
            return out
    except Exception as exc:  # noqa: BLE001
        out["status"] = f"{type(exc).__name__}"

    # Autodiscovery from the site root.
    root = f"{urlparse(url).scheme}://{urlparse(url).netloc}/"
    try:
        page = client.get(root)
    except Exception as exc:  # noqa: BLE001
        out["reason"] = f"site root unreachable ({type(exc).__name__})"
        return out
    if page.status_code >= 400:
        out["reason"] = f"site root also {page.status_code} — host likely gone"
        return out

    from services import feed_discovery
    try:
        cands = feed_discovery.discover_feed_urls(root, timeout=TIMEOUT) or []
    except Exception:  # noqa: BLE001
        cands = []
    # Cheap conventional guesses, only if autodiscovery found nothing.
    if not cands:
        cands = [urljoin(root, p) for p in ("feed/", "rss/", "index.xml", "atom.xml", "feed.xml")]

    for cand in cands[:6]:
        if cand == url:
            continue
        try:
            cr = client.get(cand)
        except Exception:  # noqa: BLE001
            continue
        if cr.status_code != 200 or not cr.content:
            continue
        parsed = feedparser.parse(cr.content)
        if not getattr(parsed, "feed", None):
            continue
        ok, why = _looks_like_same_feed(parsed, url, stored_title)
        if ok:
            out.update(candidate=cand, reason=why, verdict="replacement")
            return out
        out["reason"] = why
    out["reason"] = out["reason"] or "no feed found on the site"
    return out


def main_cli(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--json")
    args = ap.parse_args(argv)

    base = f"/data/users/{args.user}"
    m = sqlite3.connect(f"file:{base}/lectio_meta.sqlite3?mode=ro", uri=True)
    r = sqlite3.connect(f"file:{base}/lectio_reader.sqlite?mode=ro", uri=True)
    live = {u: t for u, t in r.execute("SELECT url, title FROM feeds")}
    targets = [(u, live.get(u, "")) for u, e in
               m.execute("SELECT feed_url,last_error FROM feed_failure_state "
                         "WHERE consecutive_failures>0 AND last_error IS NOT NULL")
               if u in live and "404" in (e or "")]
    if args.limit:
        targets = targets[:args.limit]
    print(f"probing {len(targets)} feeds\n", flush=True)

    results = []
    with httpx.Client(headers={"User-Agent": UA}, timeout=TIMEOUT,
                      follow_redirects=True) as client:
        for i, (url, title) in enumerate(targets, 1):
            res = probe_one(client, url, title)
            results.append(res)
            mark = {"replacement": "FOUND", "recovered": "ALIVE", "dead": "dead "}[res["verdict"]]
            print(f"  [{i:>3}/{len(targets)}] {mark}  {url[:64]}", flush=True)
            if res["candidate"]:
                print(f"              -> {res['candidate'][:70]}  ({res['reason']})", flush=True)
            time.sleep(args.delay)

    n = {k: sum(1 for x in results if x["verdict"] == k) for k in ("replacement", "recovered", "dead")}
    print(f"\nreplacement found: {n['replacement']}   original alive again: {n['recovered']}   "
          f"no feed found: {n['dead']}")
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(results, fh, indent=2)
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
