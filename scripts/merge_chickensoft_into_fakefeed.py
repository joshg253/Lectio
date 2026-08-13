"""Merge the old Chickensoft RSS feed into the Page Feed that replaced it.

chickensoft.games/blog/rss.xml is one of the 404s from the sweep. It was
unsubscribed-with-keep, so it holds no folder row and shows nowhere in
Settings → Feeds → Folders — but it still exists in reader with 4 entries, 1
star and 7 manual tags, reachable through the Kept view. A Page Feed was then
built for the same blog and picked up 10 posts, a strict superset of the old 4
(matched on link).

So this is a combine, not an unsubscribe: every entry moves onto the survivor
with its tags, star, read state, capture and per-entry meta, and only then is
the old feed deleted. ``purge_orphaned_feed(migrate_curation_to=…)`` is exactly
that path — the same one feed dedup uses — and it matches by GUID first, then by
normalized link, synthesizing only what neither matches.

The old feed is already orphaned (no folder_feeds rows), which is the
precondition purge_orphaned_feed documents.

Usage:
    LECTIO_DATA_DIR=/data uv run scripts/merge_chickensoft_into_fakefeed.py \
        --user <user_id> --keep <survivor_feed_url> [--apply]

Defaults to a dry run; --apply performs the merge.
"""
from __future__ import annotations

import argparse
import sys

sys.path.insert(0, "/app")

import main  # noqa: E402

OLD = "https://chickensoft.games/blog/rss.xml"


def run(keep_url: str, apply: bool) -> None:
    with main.get_reader() as reader:
        old = reader.get_feed(OLD, None)
        new = reader.get_feed(keep_url, None)
        if old is None:
            print(f"{OLD} is not in reader — nothing to merge")
            return
        if new is None:
            print(f"survivor {keep_url} is not in reader — refusing")
            return
        old_entries = list(reader.get_entries(feed=OLD))
        new_links = {str(e.link or "") for e in reader.get_entries(feed=keep_url)}

    print(f"old:  {OLD}  ({len(old_entries)} entries)")
    print(f"keep: {keep_url}  ({len(new_links)} entries)")
    for e in old_entries:
        link = str(e.link or "")
        starred = main._entry_is_starred(OLD, str(e.id))
        tags = main.get_manual_tags_for_entry(OLD, str(e.id))
        match = "matches" if link in new_links else "SYNTHESIZED"
        print(f"   {match:12} star={int(starred)} tags={sorted(tags)} {link}")

    with main.get_meta_connection() as conn:
        in_folders = conn.execute(
            "SELECT COUNT(*) FROM folder_feeds WHERE feed_url = ?", (OLD,)
        ).fetchone()[0]
    print(f"old feed folder rows: {in_folders} (must be 0 to be orphaned)")
    if in_folders:
        print("refusing: not orphaned")
        return

    if not apply:
        print("\nDRY RUN — pass --apply to merge.")
        return

    with main.get_reader() as reader:
        with main.get_meta_connection() as conn:
            main.purge_orphaned_feed(
                reader, conn, OLD,
                archive_pending=True,
                migrate_curation_to=keep_url,
            )
            conn.commit()
    main.invalidate_meta_structure_cache()

    with main.get_reader() as reader:
        survivors = list(reader.get_entries(feed=keep_url))
    kept_tags = sum(1 for e in survivors if main.get_manual_tags_for_entry(keep_url, str(e.id)))
    kept_stars = sum(1 for e in survivors if main._entry_is_starred(keep_url, str(e.id)))
    print(f"\nmerged. survivor now has {len(survivors)} entries, "
          f"{kept_tags} tagged, {kept_stars} starred.")


def main_cli() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True)
    ap.add_argument("--keep", required=True, help="survivor feed URL (the Page Feed)")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    main._run_in_user_context(args.user, lambda: run(args.keep, args.apply))


if __name__ == "__main__":
    main_cli()
