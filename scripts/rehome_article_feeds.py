"""Move captures off "feeds" that are really article URLs, onto Saved Articles.

29 feeds in the live library are article pages, not feeds — added by hand while
searching for a real one, most likely. Each holds one or two user captures, and
each is a permanently failing feed: Lectio re-parses that article page as RSS on
every cycle and logs a ParseError.

The captures belong on Saved Articles. What makes this worth a script rather
than 29 rounds of Move-to-feed is that the entry's key changes, and four stores
reference it:

  - reader (star, manual tags, read state) — handled by `main._move_entry_to_feed`,
    which also carries the BODY across, synthesizing the target entry under the
    SAME entry_id so only feed_url changes;
  - the starred archive (offline HTML + every captured image) — **not** handled
    by that function, which is why this script exists at all. `rekey_archive`
    repoints it; without that step the move silently orphans the offline copy
    and the post's images go with the deleted feed;
  - `entry_lead_images`, and the date/title/link/content override tables, all
    keyed on (feed_url, entry_id).

Order matters: move, then re-key the archive and the meta rows, then delete the
husk feed. Deleting first would take the source entry with it.

    uv run python scripts/rehome_article_feeds.py            # dry run
    uv run python scripts/rehome_article_feeds.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from services import saved_articles as saved_articles_service  # noqa: E402
from services import tenancy  # noqa: E402

# A URL with any of these reads as a feed address, whatever it returns.
_FEEDISH_RE = re.compile(
    r"(feed|rss|atom|\.xml|/index\.xml|newsfeed|headlines|\.rdf)", re.I)
# Never candidates: the synthetic saved feed, and social URLs that are a
# different (deliberate) kind of non-feed subscription.
_NEVER = ("lectio:saved",)
_NEVER_HOSTS = ("twitter.com", "x.com", "nitter.")

# Meta tables keyed on (feed_url, entry_id) that must follow the entry.
_META_KEYED_TABLES = (
    "entry_lead_images", "entry_date_overrides", "entry_title_overrides",
    "entry_link_overrides", "entry_content_overrides", "entry_content_edits",
    "entry_read_state", "read_history", "entry_feed_tags", "saved_entries",
    "archived_entries",
)


def find_article_feeds() -> list[tuple[str, list[str]]]:
    """[(feed_url, [entry_id, …])] for feeds that are really article pages.

    Requires ALL of: a recorded parse failure, a URL with no feed marker, and
    every entry being a user capture. A dead *real* feed carrying saved captures
    matches the last two but not the URL test — that is the distinction, and it
    is why the URL shape is load-bearing rather than cosmetic.
    """
    out: list[tuple[str, list[str]]] = []
    with main.get_reader() as reader:
        db = reader._storage.get_db()
        rows = db.execute(
            "SELECT url FROM feeds WHERE last_exception IS NOT NULL"
        ).fetchall()
        for row in rows:
            url = str(row[0])
            if url in _NEVER or _FEEDISH_RE.search(url):
                continue
            if any(host in url for host in _NEVER_HOSTS):
                continue
            entries = db.execute(
                "SELECT id, added_by FROM entries WHERE feed = ?", (url,)
            ).fetchall()
            if not entries or any(str(e[1]) != "user" for e in entries):
                continue
            out.append((url, [str(e[0]) for e in entries]))
    return out


def rehome_for_user(user_id: str, apply: bool, limit: int = 0) -> int:
    target = saved_articles_service.SAVED_FEED_URL
    feeds = find_article_feeds()
    if limit:
        feeds = feeds[:limit]
    total = sum(len(ids) for _u, ids in feeds)
    print(f"[{user_id}] {len(feeds)} article-URL feed(s) holding {total} capture(s)",
          flush=True)
    for url, ids in feeds[:5]:
        print(f"    {len(ids)} × {url[:76]}", flush=True)
    if not feeds:
        return 0
    if not apply:
        print("  dry run — re-run with --apply to move them", flush=True)
        return total

    moved: list[dict] = []
    for url, ids in feeds:
        for entry_id in ids:
            try:
                with main.get_reader() as reader:
                    with main.get_meta_connection() as conn:
                        result = main._move_entry_to_feed(reader, conn, url, entry_id, target)
            except Exception as exc:  # noqa: BLE001
                print(f"    SKIP {entry_id[:60]}: {type(exc).__name__}: {exc}", flush=True)
                continue
            if not result.get("ok"):
                print(f"    SKIP {entry_id[:60]}: {result.get('error')}", flush=True)
                continue

            # The archive is the reason this is a script: _move_entry_to_feed
            # does not touch it, so without this the offline copy and every
            # captured image stay keyed to the feed we are about to delete.
            try:
                main.starred_archive_service.rekey_archive(url, entry_id, target, entry_id)
            except Exception as exc:  # noqa: BLE001
                print(f"    archive re-key failed for {entry_id[:50]}: {exc}", flush=True)

            with main.get_meta_connection() as conn:
                for table in _META_KEYED_TABLES:
                    try:
                        conn.execute(
                            f"UPDATE OR IGNORE {table} SET feed_url = ? "
                            "WHERE feed_url = ? AND entry_id = ?",
                            (target, url, entry_id),
                        )
                    except sqlite3.OperationalError:
                        pass      # table absent on this tenant

            moved.append({"from_feed": url, "entry_id": entry_id, "to_feed": target,
                          "synthesized": bool(result.get("synth")),
                          "star": bool(result.get("star")), "tags": result.get("tags", 0)})
            print(f"    moved {entry_id[:64]}", flush=True)

        # Husk removed only after its entries are re-homed.
        try:
            with main.get_reader() as reader:
                reader.delete_feed(url)
            print(f"  removed feed {url[:70]}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  could not remove {url[:60]}: {exc}", flush=True)

    print(f"  moved {len(moved)} capture(s)", flush=True)
    if moved:
        out = tenancy.meta_db_path().parent / f"rehomed_article_feeds_{datetime.now():%Y%m%d-%H%M%S}.json"
        out.write_text(json.dumps(moved, indent=2))
        print(f"  Log: {out}", flush=True)
    return len(moved)


def main_cli() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="move them (default: dry run)")
    ap.add_argument("--user", default=None, help="restrict to one user_id")
    ap.add_argument("--limit", type=int, default=0, help="only the first N feeds")
    args = ap.parse_args()

    for uid in ([args.user] if args.user else main._background_user_ids()):
        with tenancy.user_context(uid):
            rehome_for_user(uid, args.apply, args.limit)
    if args.apply:
        print("\nRestart the app so nothing serves a cached render.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
