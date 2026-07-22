"""Tag-as-keep backfill migration (Part C of the tag-as-keep epic).

After the tag-as-keep flip, a post is *kept* (offline-archived, never pruned)
whenever it is starred OR manually tagged. This one-off script brings existing
curation up to that contract:

1. **Retro-archive** — every manually-tagged entry that has no `complete` row in
   the starred-archive DB gets an ``enqueue_archive`` (the live worker then
   captures it). Star-only entries were already archived, so this covers the
   tag axis added by the flip.
2. **Wayback backfill** — genuinely-empty curated posts (starred OR tagged with
   < MIN_CONTENT_CHARS of stored content, e.g. a long-dead feed) are refilled
   from the closest Archive.org snapshot: availability API -> fetch snapshot ->
   readability-extract -> write reader ``entries.content``.

Runs per user (each under its own tenancy context). DB-only; safe to re-run
(idempotent: already-archived / already-filled entries are skipped).

Usage (inside the app container so LECTIO_DATA_DIR=/data resolves):
    # READ-ONLY report (default) — counts only, no writes, no network:
    uv run scripts/migrate_tag_as_keep.py
    # add Wayback availability probing to the report (read-only network):
    uv run scripts/migrate_tag_as_keep.py --probe-wayback
    # WRITES (after Josh reviews the dry-run):
    uv run scripts/migrate_tag_as_keep.py --apply
    # restrict to one user / one pass / cap Wayback fetches:
    uv run scripts/migrate_tag_as_keep.py --apply --user u_x --only wayback --limit 50
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from typing import Iterable
from urllib.parse import urlparse

import httpx

# Allow `uv run scripts/migrate_tag_as_keep.py` (script dir on sys.path[0]) to
# still import the top-level app module.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from services import tenancy  # noqa: E402

MIN_CONTENT_CHARS = 300
WAYBACK_AVAIL_API = "https://archive.org/wayback/available"
UA = main.READABILITY_USER_AGENT


# --------------------------------------------------------------------------- #
# Read helpers (per bound tenancy user)
# --------------------------------------------------------------------------- #
def _tagged_keys() -> set[tuple[str, str]]:
    with sqlite3.connect(str(tenancy.reader_db_path()), timeout=10.0) as rc:
        return {
            (str(f), str(i))
            for f, i in rc.execute(
                "SELECT feed, id FROM entry_tags WHERE key LIKE ?",
                (f"{main.MANUAL_TAG_KEY_PREFIX}%",),
            )
        }


def _starred_keys() -> set[tuple[str, str]]:
    with sqlite3.connect(str(tenancy.meta_db_path()), timeout=10.0) as mc:
        return {
            (str(f), str(i))
            for f, i in mc.execute("SELECT feed_url, entry_id FROM saved_entries")
        }


def _is_youtube_feed(feed_url: str) -> bool:
    # YouTube entries (add-to-playlist → watch → retention-purge) are ephemeral
    # video links with legitimately short descriptions — pointless to archive or
    # Wayback-fill, and they inflate the "empty" count. Always excluded.
    #
    # Matched on the parsed host via main's canonical check, which is an exact
    # suffix test: the previous `"youtube.com" in feed_url` substring form also
    # matched `youtube.com.evil.com` and `notyoutube.com`.
    return main._is_youtube_host(urlparse(feed_url).netloc)


def _kept_feed_urls() -> set[str]:
    with sqlite3.connect(str(tenancy.meta_db_path()), timeout=10.0) as mc:
        try:
            return {str(r[0]) for r in mc.execute("SELECT feed_url FROM kept_feeds")}
        except sqlite3.Error:
            return set()


def _at_risk_feeds(dead_threshold: int) -> set[str]:
    """Feeds whose curated posts are actually at risk: unsubscribed-but-kept
    (kept_feeds) plus persistently-failing ("dead") feeds. Live feeds' content is
    still fetchable on demand, so they're deferred to a later --scope all run."""
    at_risk: set[str] = set()
    with sqlite3.connect(str(tenancy.meta_db_path()), timeout=10.0) as mc:
        try:
            at_risk |= {str(r[0]) for r in mc.execute("SELECT feed_url FROM kept_feeds")}
        except sqlite3.Error:
            pass
        try:
            at_risk |= {
                str(r[0]) for r in mc.execute(
                    "SELECT feed_url FROM feed_failure_state WHERE consecutive_failures >= ?",
                    (dead_threshold,),
                )
            }
        except sqlite3.Error:
            pass
    return at_risk


def _complete_archive_keys() -> set[tuple[str, str]]:
    try:
        with sqlite3.connect(str(tenancy.starred_archive_db_path()), timeout=10.0) as ac:
            return {
                (str(f), str(i))
                for f, i in ac.execute(
                    "SELECT feed_url, entry_id FROM archived_entry WHERE status = 'complete'"
                )
            }
    except sqlite3.Error:
        return set()


def _content_len(rc: sqlite3.Connection, feed: str, entry_id: str) -> int:
    """Length of the stored HTML for a reader entry (0 if missing/empty)."""
    row = rc.execute("SELECT content FROM entries WHERE feed = ? AND id = ?", (feed, entry_id)).fetchone()
    if not row or not row[0]:
        return 0
    try:
        parts = json.loads(row[0])
        return sum(len(str(p.get("value") or "")) for p in parts if isinstance(p, dict))
    except (json.JSONDecodeError, TypeError):
        return len(str(row[0]))


def _failure_info() -> dict[str, tuple[int, str, float | None]]:
    """feed_url -> (consecutive_failures, last_error, last_success_at)."""
    out: dict[str, tuple[int, str, float | None]] = {}
    with sqlite3.connect(str(tenancy.meta_db_path()), timeout=10.0) as mc:
        try:
            for url, fails, err, succ in mc.execute(
                "SELECT feed_url, consecutive_failures, last_error, last_success_at FROM feed_failure_state"
            ):
                out[str(url)] = (int(fails or 0), str(err or ""), succ)
        except sqlite3.Error:
            pass
    return out


def _feed_titles() -> dict[str, str]:
    """feed_url -> display title (user_title preferred), for readable reports."""
    try:
        with sqlite3.connect(str(tenancy.reader_db_path()), timeout=10.0) as rc:
            return {
                str(url): str(ut or t or url)
                for url, t, ut in rc.execute("SELECT url, title, user_title FROM feeds")
            }
    except sqlite3.Error:
        return {}


def _entry_link(rc: sqlite3.Connection, feed: str, entry_id: str) -> str | None:
    row = rc.execute("SELECT link FROM entries WHERE feed = ? AND id = ?", (feed, entry_id)).fetchone()
    link = (row[0] if row else None) or entry_id
    return link if str(link).startswith(("http://", "https://")) else None


# --------------------------------------------------------------------------- #
# Wayback
# --------------------------------------------------------------------------- #
def _wayback_snapshot(client: httpx.Client, url: str) -> str | None:
    """Return the closest available Archive.org snapshot URL, or None."""
    try:
        r = client.get(WAYBACK_AVAIL_API, params={"url": url})
        r.raise_for_status()
        snap = (r.json().get("archived_snapshots") or {}).get("closest") or {}
        if snap.get("available") and snap.get("url"):
            return str(snap["url"])
    except Exception:  # noqa: BLE001
        pass
    return None


def _write_content(feed: str, entry_id: str, html: str) -> None:
    content_json = json.dumps([{"value": html, "type": "text/html", "language": None}])
    with sqlite3.connect(str(tenancy.reader_db_path()), timeout=10.0) as rc:
        rc.execute("UPDATE entries SET content = ? WHERE feed = ? AND id = ?", (content_json, feed, entry_id))
        rc.commit()


# --------------------------------------------------------------------------- #
# Passes
# --------------------------------------------------------------------------- #
def _empty_curated(curated: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    with sqlite3.connect(str(tenancy.reader_db_path()), timeout=10.0) as rc:
        for feed, eid in curated:
            if _content_len(rc, feed, eid) < MIN_CONTENT_CHARS:
                out.append((feed, eid))
    return out


def run_for_user(uid: str, args) -> dict:
    tagged = _tagged_keys()
    starred = _starred_keys()
    complete = _complete_archive_keys()

    # Always drop YouTube (ephemeral video links). When scope is dead-unsub
    # (default) restrict to unsubscribed-kept + persistently-dead feeds — the
    # curation that's actually at risk of losing its source.
    at_risk = _at_risk_feeds(args.dead_threshold) if args.scope == "dead-unsub" else None

    def _keep(key: tuple[str, str]) -> bool:
        feed = key[0]
        if _is_youtube_feed(feed):
            return False
        if at_risk is not None and feed not in at_risk:
            return False
        return True

    tagged = {k for k in tagged if _keep(k)}
    starred = {k for k in starred if _keep(k)}
    curated = tagged | starred

    tagged_missing = sorted(tagged - complete)
    empties = _empty_curated(curated)

    if args.list_dead_feeds:
        curated_by_feed: dict[str, int] = {}
        for feed, _eid in curated:
            curated_by_feed[feed] = curated_by_feed.get(feed, 0) + 1
        titles = _feed_titles()
        kept = _kept_feed_urls()
        fail = _failure_info()
        feeds = at_risk if at_risk is not None else set(curated_by_feed)
        feeds = {f for f in feeds if not _is_youtube_feed(f)}
        import datetime as _dt

        def _succ(ts):
            if not ts:
                return "never"
            try:
                return _dt.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")
            except (ValueError, OSError):
                return "?"
        print(f"  at-risk feeds ({len(feeds)}), by curated posts at stake:")
        for feed in sorted(feeds, key=lambda f: curated_by_feed.get(f, 0), reverse=True):
            fails, err, succ = fail.get(feed, (0, "", None))
            flag = "kept" if feed in kept else "dead"
            print(f"    curated={curated_by_feed.get(feed, 0):>4}  [{flag}]  fails={fails:>3}  last_ok={_succ(succ)}  "
                  f"{titles.get(feed, feed)}  <{feed}>  err={err[:60]!r}")
        print()

    if args.list_empty_feeds and empties:
        by_feed: dict[str, int] = {}
        for feed, _eid in empties:
            by_feed[feed] = by_feed.get(feed, 0) + 1
        titles = _feed_titles()
        kept = _kept_feed_urls()  # to flag unsub-kept vs dead
        print(f"  empty-curated posts by feed ({len(empties)} across {len(by_feed)} feeds):")
        for feed, n in sorted(by_feed.items(), key=lambda kv: kv[1], reverse=True):
            flag = "kept" if feed in kept else "dead"
            print(f"    {n:>5}  [{flag}]  {titles.get(feed, feed)}  <{feed}>")
        print()

    stats = {
        "tagged": len(tagged), "starred": len(starred), "curated": len(curated),
        "tagged_missing_archive": len(tagged_missing), "empty_curated": len(empties),
        "archived_enqueued": 0, "wayback_filled": 0, "wayback_available": 0, "wayback_no_snapshot": 0,
    }

    do_archive = args.only in ("all", "archive")
    do_wayback = args.only in ("all", "wayback")

    # Pass 1 — retro-archive.
    if do_archive and args.apply:
        for feed, eid in tagged_missing:
            try:
                main.starred_archive_service.enqueue_archive(feed, eid)
                stats["archived_enqueued"] += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  [archive] enqueue failed {feed} / {eid}: {exc}")

    # Pass 2 — Wayback backfill (or availability probe in dry-run).
    if do_wayback and (args.apply or args.probe_wayback):
        limit = args.limit if args.limit > 0 else len(empties)
        with httpx.Client(follow_redirects=True, timeout=20.0, headers={"User-Agent": UA}) as client, \
                sqlite3.connect(str(tenancy.reader_db_path()), timeout=10.0) as rc:
            for feed, eid in empties[:limit]:
                link = _entry_link(rc, feed, eid)
                if not link:
                    continue
                snap = _wayback_snapshot(client, link)
                if not snap:
                    stats["wayback_no_snapshot"] += 1
                    time.sleep(args.sleep)
                    continue
                stats["wayback_available"] += 1
                if args.apply:
                    try:
                        page = client.get(snap)
                        page.raise_for_status()
                        _title, html = main.extract_readability_article(page.text, link)
                        if html and len(html) >= MIN_CONTENT_CHARS:
                            _write_content(feed, eid, html)
                            stats["wayback_filled"] += 1
                    except Exception as exc:  # noqa: BLE001
                        print(f"  [wayback] fill failed {feed} / {eid}: {exc}")
                time.sleep(args.sleep)

    return stats


def main_cli() -> None:
    ap = argparse.ArgumentParser(description="Tag-as-keep backfill (retro-archive + Wayback).")
    ap.add_argument("--apply", action="store_true", help="Perform writes (default: dry-run report only).")
    ap.add_argument("--probe-wayback", action="store_true", help="Dry-run: probe Archive.org availability (read-only network).")
    ap.add_argument("--only", choices=["all", "archive", "wayback"], default="all")
    ap.add_argument("--scope", choices=["dead-unsub", "all"], default="dead-unsub",
                    help="dead-unsub (default): only unsubscribed-kept + dead feeds. all: whole library.")
    ap.add_argument("--dead-threshold", type=int, default=10,
                    help="consecutive_failures for a feed to count as 'dead' (default 10).")
    ap.add_argument("--list-empty-feeds", action="store_true",
                    help="Dry-run: print the empty-curated (Wayback candidate) post count per feed.")
    ap.add_argument("--list-dead-feeds", action="store_true",
                    help="Dry-run: list at-risk (dead/unsub) feeds with curated-post count + failure info.")
    ap.add_argument("--user", default=None, help="Restrict to one user_id (default: all enabled users).")
    ap.add_argument("--limit", type=int, default=0, help="Cap Wayback URL lookups (0 = no cap).")
    ap.add_argument("--sleep", type=float, default=1.0, help="Seconds between Archive.org requests (throttle).")
    args = ap.parse_args()

    users = [args.user] if args.user else main._background_user_ids()
    mode = "APPLY (writing)" if args.apply else "DRY-RUN (read-only)"
    print(f"tag-as-keep backfill — {mode} — scope={args.scope} (YouTube always excluded) — users: {users}\n")

    totals: dict[str, int] = {}
    for uid in users:
        with tenancy.user_context(uid):
            s = run_for_user(uid, args)
        print(f"[{uid}]")
        print(f"  tagged={s['tagged']}  starred={s['starred']}  curated(star|tag)={s['curated']}")
        print(f"  tagged missing archive (retro-archive candidates): {s['tagged_missing_archive']}")
        print(f"  empty curated (<{MIN_CONTENT_CHARS} chars, Wayback candidates): {s['empty_curated']}")
        if args.probe_wayback or args.apply:
            print(f"  wayback available={s['wayback_available']}  no-snapshot={s['wayback_no_snapshot']}")
        if args.apply:
            print(f"  APPLIED: archives enqueued={s['archived_enqueued']}  wayback filled={s['wayback_filled']}")
        print()
        for k, v in s.items():
            totals[k] = totals.get(k, 0) + v

    print("TOTALS:", {k: totals[k] for k in (
        "tagged_missing_archive", "empty_curated", "wayback_available",
        "wayback_no_snapshot", "archived_enqueued", "wayback_filled") if k in totals})
    if not args.apply:
        print("\nDry-run only — no changes made. Re-run with --apply to write.")


if __name__ == "__main__":
    main_cli()
