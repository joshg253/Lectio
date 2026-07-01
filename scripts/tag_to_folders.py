"""Promote each manual tag on uncategorized feeds to its own folder.

Folder name = the tag with only its FIRST LETTER capitalized (so `python`->`Python`,
`linux-stuff`->`Linux-stuff`, `c++`->`C++`, `pshell`->`Pshell`). The deliberately
odd casing makes these auto-created tag-folders obvious to review/merge later.

A feed goes into a folder for EACH of its tags (multi-membership, mirroring the
tags). Untagged uncategorized feeds are left in Uncategorized. Existing folders
are reused case-insensitively (no duplicates). New folders are created one level
below the root "All Feeds" folder, matching Lectio's flat hierarchy.

Usage:
    uv run python -m scripts.tag_to_folders --data-dir data --user u_x [--dry-run]
"""

from __future__ import annotations

import argparse
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

PFX = "lectio.manual_tag."
ROOT_NAME = "All Feeds"


def _db_paths(data_dir: Path, user: str | None) -> tuple[Path, Path]:
    base = data_dir / "users" / user if user else data_dir
    return base / "lectio_reader.sqlite", base / "lectio_meta.sqlite3"


def _cap(tag: str) -> str:
    return tag[:1].upper() + tag[1:]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", default="data")
    p.add_argument("--user", default=None)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    reader, meta = _db_paths(Path(args.data_dir), args.user)
    rc = sqlite3.connect(reader)
    mc = sqlite3.connect(meta)

    root_row = mc.execute("SELECT id FROM folders WHERE name=? AND parent_id IS NULL", (ROOT_NAME,)).fetchone()
    if not root_row:
        raise SystemExit("root folder not found")
    root_id = root_row[0]
    name_lc_to_id = {r[1].lower(): r[0] for r in mc.execute("SELECT id, name FROM folders")}

    foldered = {r[0] for r in mc.execute("SELECT DISTINCT feed_url FROM folder_feeds")}
    all_feeds = {r[0] for r in rc.execute("SELECT url FROM feeds")}
    uncat = all_feeds - foldered

    feed_tags: dict[str, set[str]] = defaultdict(set)
    for feed, key in rc.execute(f"SELECT DISTINCT feed, key FROM entry_tags WHERE key LIKE '{PFX}%'"):
        if feed in uncat:
            feed_tags[feed].add(key[len(PFX):])

    tags = sorted({t for ts in feed_tags.values() for t in ts})
    membership_per_feed = Counter(len(ts) for ts in feed_tags.values())

    created = reused = memberships = 0
    tag_to_folder_id: dict[str, int] = {}
    for t in tags:
        name = _cap(t)
        fid = name_lc_to_id.get(name.lower())
        if fid is None:
            if not args.dry_run:
                cur = mc.execute("INSERT INTO folders (name, parent_id) VALUES (?, ?)", (name, root_id))
                fid = cur.lastrowid
                name_lc_to_id[name.lower()] = fid
            created += 1
        else:
            reused += 1
        tag_to_folder_id[t] = fid or -1

    for feed, ts in feed_tags.items():
        for t in ts:
            fid = tag_to_folder_id.get(t)
            if fid and fid > 0 and not args.dry_run:
                mc.execute("INSERT OR IGNORE INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)", (fid, feed))
            memberships += 1

    if not args.dry_run:
        mc.commit()

    print(("DRY-RUN " if args.dry_run else "") + f"tags/folders: {len(tags)}  (create={created}, reuse-existing={reused})")
    print(f"tagged uncategorized feeds: {len(feed_tags)}  ->  folder memberships added: {memberships}")
    print(f"untagged uncategorized feeds (left in Uncategorized): {len(uncat) - len(feed_tags)}")
    print("feeds by #tags(=#folders they land in): " +
          ", ".join(f"{k}tag:{v}" for k, v in sorted(membership_per_feed.items())))
    if not args.dry_run:
        print("\nDone. Restart the app to refresh the sidebar: docker compose restart")


if __name__ == "__main__":
    main()
