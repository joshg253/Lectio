"""Clear a stale entry_content_overrides pin and resync reader from a FakeFeedz
link_list feed's own scraped_entries.content.

Background: a manual "Refetch content"/"Refetch page" click writes
entry_content_overrides so a later feed refresh can't clobber the "better" copy
it pulled -- see services/saved_articles.py's replace_entry_content /
refresh_captured_article, and feed_refresh.py's reapply_entry_content_overrides
(runs after every refresh, re-stamping the pinned content back onto reader's
entries table).

That's backwards for a FakeFeedz link_list feed once a content_selector is
added (see services/scraper_service.py's _new_entry_extras): the selector
extracts a clean per-entry body directly from the page at scrape time, but a
pin created BEFORE the selector existed -- readability's or full-page's whole-
chromed-page guess -- keeps winning over the feed's own now-correct content on
every refresh, since reapply_entry_content_overrides has no way to know the
pin predates a fix.

Confirmed live 2026-09-19 on texasbluesalley.com's "Free Blues Guitar Lessons"
page feed: 9 of 10 entries had a ~36KB override (the old readability/full-page
guess) sitting on top of a correctly short (~1-2KB) scraped_entries.content.

Dry run by default -- reports what would change. --apply deletes the stale
override rows for the given feed and writes scraped_entries.content into
reader's entries.content directly (same technique
reapply_entry_content_overrides itself uses, just restoring the good copy).

Usage:
    uv run scripts/clear_stale_content_overrides.py --user <uid> --feed-url <file://...>
    uv run scripts/clear_stale_content_overrides.py --user <uid> --feed-url <file://...> --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from services import scraper_service, tenancy  # noqa: E402


def run(uid: str, feed_url: str, apply: bool) -> None:
    feed_id = scraper_service.scraped_feed_id_from_url(feed_url)
    if not feed_id:
        print(f"[{uid}] {feed_url} is not a FakeFeedz scraped-feed URL -- nothing to do.")
        return

    with tenancy.user_context(uid):
        with main.get_meta_connection() as conn:
            override_rows = conn.execute(
                "SELECT entry_id, LENGTH(content) AS len FROM entry_content_overrides WHERE feed_url = ?",
                (feed_url,),
            ).fetchall()
            scraped_rows = {
                r["id"]: r["content"]
                for r in conn.execute("SELECT id, content FROM scraped_entries WHERE scraped_feed_id = ?", (feed_id,)).fetchall()
            }

        if not override_rows:
            print(f"[{uid}] no entry_content_overrides rows for {feed_url} -- nothing to do.")
            return

        print(f"[{uid}] {len(override_rows)} override row(s) found for {feed_url}:")
        for r in override_rows:
            has_scraped = r["entry_id"] in scraped_rows
            print(f"  {r['entry_id']}: override is {r['len']} bytes; scraped_entries content available: {has_scraped}")

        if not apply:
            print(f"[{uid}] dry run -- pass --apply to delete these overrides and resync from scraped_entries.content.")
            return

        with main.get_meta_connection() as conn:
            deleted = conn.execute("DELETE FROM entry_content_overrides WHERE feed_url = ?", (feed_url,)).rowcount
            conn.commit()

        with main.get_reader() as reader:
            db = reader._storage.get_db()
            updated = 0
            for entry_id, content in scraped_rows.items():
                content_json = json.dumps([{"value": content or "", "type": "text/html", "language": None}])
                cur = db.execute(
                    "UPDATE entries SET content = ? WHERE feed = ? AND id = ?",
                    (content_json, feed_url, entry_id),
                )
                updated += cur.rowcount
            db.commit()

        print(f"[{uid}] deleted {deleted} override row(s), resynced {updated} entr(y/ies) from scraped_entries.content.")


def main_cli() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", required=True, help="user_id to operate on")
    ap.add_argument("--feed-url", required=True, help="the FakeFeedz file:// feed URL to fix")
    ap.add_argument("--apply", action="store_true", help="delete stale overrides and resync (default: dry run)")
    args = ap.parse_args()
    run(args.user, args.feed_url, args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
