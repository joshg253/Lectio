"""Star a dead feed's curated entries, capture them, then unsubscribe it.

The tail of the 404 sweep. Three feeds were left unremoved because their
curation had not been checked:

    appdividend.com/category/cpp-c/feed/   37 entries, all tagged "c++"
    blog.box.com/feed/                      1 entry, tagged "job-stuff"
    blog.hipmunk.com/feed/                  0 entries

**A tag is a keep signal, but only a star is archived.** That asymmetry is the
whole reason this script exists: an entry that is tagged and unstarred has its
curation recorded and its *contents* nowhere, so unsubscribing would leave the
tag pointing at a page on a domain that 404s. Starring first runs the offline
capture, and the capture is what survives the feed.

Order matters and is not negotiable:

  1. star every entry with a keep signal (``apply_star_state``), which enqueues
     its capture;
  2. drain the queue synchronously (``force_archive_pending_for_feed``) — the
     background worker may not run at all in a one-off script process, and
     ``purge_orphaned_feed``'s own flush is bounded at 60s, which 37 entries can
     outlast;
  3. only then remove folder rows and purge.

Doing 3 before 2 is how a capture becomes a content-less archive shell.

Usage:
    LECTIO_DATA_DIR=/data uv run scripts/star_then_unsubscribe.py \
        --user <user_id> [--apply]

Defaults to a dry run; --apply stars, captures and unsubscribes.
"""
from __future__ import annotations

import argparse
import sys

sys.path.insert(0, "/app")

import main  # noqa: E402

FEEDS = (
    "https://appdividend.com/category/cpp-c/feed/",
    "https://blog.box.com/feed/",
    "https://blog.hipmunk.com/feed/",
)

# Generous: each capture is a page fetch plus its images. Bounded so a hung host
# cannot stall the run forever.
CAPTURE_SECONDS = 600.0


def _curated(feed_url: str) -> list[str]:
    """Entry ids worth keeping: anything with a star, a manual tag, or an archive."""
    out: list[str] = []
    with main.get_reader() as reader:
        for entry in reader.get_entries(feed=feed_url):
            eid = str(entry.id)
            starred = main._entry_is_starred(feed_url, eid)
            if main.entry_has_keep_signal(feed_url, eid, starred=starred):
                out.append(eid)
    return out


def run(apply: bool) -> None:
    for feed_url in FEEDS:
        with main.get_reader() as reader:
            exists = reader.get_feed(feed_url, None) is not None
            total = sum(1 for _ in reader.get_entries(feed=feed_url)) if exists else 0
        if not exists:
            print(f"\n{feed_url}\n   not subscribed — nothing to do")
            continue

        curated = _curated(feed_url)
        already = [e for e in curated if main._entry_is_starred(feed_url, e)]
        print(f"\n{feed_url}")
        print(f"   entries={total}  curated={len(curated)}  already starred={len(already)}")

        if not apply:
            continue

        newly = 0
        for eid in curated:
            if main._entry_is_starred(feed_url, eid):
                continue
            main.apply_star_state(feed_url, eid, True)
            newly += 1
        print(f"   starred {newly} entr{'y' if newly == 1 else 'ies'}")

        captured = main.starred_archive_service.force_archive_pending_for_feed(
            feed_url, max_seconds=CAPTURE_SECONDS
        )
        print(f"   captured {captured}")

        with main.get_meta_connection() as conn:
            conn.execute("DELETE FROM folder_feeds WHERE feed_url = ?", (feed_url,))
            conn.commit()
        with main.get_reader() as reader:
            with main.get_meta_connection() as conn:
                main.purge_orphaned_feed(reader, conn, feed_url, archive_pending=True)
                conn.commit()
        main.invalidate_meta_structure_cache()
        print("   unsubscribed")

    if not apply:
        print("\nDRY RUN — pass --apply to star, capture and unsubscribe.")


def main_cli() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    main._run_in_user_context(args.user, lambda: run(args.apply))


if __name__ == "__main__":
    main_cli()
