"""Re-attach offline captures whose feed was removed by a combine.

Combining feeds (`purge_orphaned_feed` with `migrate_curation_to`) moves the
source feed's stars and manual tags onto the survivor, but never its
**starred-archive** rows — so every capture the source feed held is left keyed
to a feed that no longer exists. The entries themselves are fine; only the
offline copies are stranded, and the Saved view then surfaces them as
archive-only *orphans*, rendered from the archive row's own stale `link` (which
is how this was noticed: a combined feed's articles still showing their old,
dead URLs).

This walks archived_entry rows whose feed_url is no longer a reader feed, finds
the feed that now holds an entry with that id, and re-keys the row (and its
asset links) onto it via the archive service's own rekey_archive.

Two judgment calls, both deliberate:

  * **Collisions keep the bigger capture.** rekey_archive refuses to clobber an
    existing target and drops the source instead — right when the target is the
    real capture, wrong when the stranded row is. This compares total stored
    bytes first and deletes the thinner of the two.
  * **The link column is refreshed** from the reader entry, because a re-keyed
    row otherwise keeps pointing at the dead domain.

Usage (inside the app container):
    uv run scripts/repair_orphaned_archives.py            # dry-run
    uv run scripts/repair_orphaned_archives.py --apply
    uv run scripts/repair_orphaned_archives.py --apply --user u_x
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from services import tenancy  # noqa: E402

_BLOB_COLS = ("source_html_zlib", "readability_html_zlib", "content_html_zlib")
_SIZE_SQL = " + ".join(f"length(COALESCE({c}, ''))" for c in _BLOB_COLS)


def find_orphans() -> list[dict]:
    """Archive rows on feeds reader no longer has, paired with the feed that
    now owns an entry of the same id. Rows whose id matches no entry anywhere
    (a genuinely deleted article) are left alone — there is nothing to attach
    them to, and deleting the only copy of a capture is not this script's call.
    """
    reader_feeds = set(main.get_all_reader_feed_urls())
    with main.get_reader() as reader:
        entry_home: dict[str, list[str]] = {}
        for e in reader.get_entries():
            entry_home.setdefault(str(e.id), []).append(str(e.feed_url))
        links = {(str(e.feed_url), str(e.id)): (e.link or "") for e in reader.get_entries()}

    conn = sqlite3.connect(str(tenancy.starred_archive_db_path()), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"SELECT feed_url, entry_id, link, title, {_SIZE_SQL} AS bytes FROM archived_entry"
        ).fetchall()
        out: list[dict] = []
        for row in rows:
            if str(row["feed_url"]) in reader_feeds:
                continue
            homes = entry_home.get(str(row["entry_id"]), [])
            if len(homes) != 1:
                continue  # no home, or ambiguous — not safe to guess
            target_feed = homes[0]
            twin = conn.execute(
                f"SELECT {_SIZE_SQL} AS bytes FROM archived_entry WHERE feed_url = ? AND entry_id = ?",
                (target_feed, row["entry_id"]),
            ).fetchone()
            out.append({
                "src_feed": str(row["feed_url"]),
                "entry_id": str(row["entry_id"]),
                "dst_feed": target_feed,
                "title": str(row["title"] or "")[:60],
                "stale_link": str(row["link"] or ""),
                "fresh_link": links.get((target_feed, str(row["entry_id"])), ""),
                "bytes": int(row["bytes"] or 0),
                "twin_bytes": int(twin["bytes"]) if twin else None,
            })
        return out
    finally:
        conn.close()


def _snapshot_deleted_blobs(orphans: list[dict]) -> None:
    """Attach the stored blobs (base64) of every row this run will delete: the
    redundant source of a drop, and the thinner target of a replace."""
    conn = sqlite3.connect(str(tenancy.starred_archive_db_path()), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        for o in orphans:
            if o["action"] == "drop-redundant":
                key = (o["src_feed"], o["entry_id"])
            elif o["action"] == "replace-thinner-twin":
                key = (o["dst_feed"], o["entry_id"])
            else:
                continue
            row = conn.execute(
                f"SELECT {', '.join(_BLOB_COLS)}, status, starred_at, archived_at, title, link"
                " FROM archived_entry WHERE feed_url = ? AND entry_id = ?", key,
            ).fetchone()
            if row is None:
                continue
            o["deleted_row"] = {
                "feed_url": key[0], "entry_id": key[1],
                "status": row["status"], "starred_at": row["starred_at"],
                "archived_at": row["archived_at"], "title": row["title"], "link": row["link"],
                **{c: (base64.b64encode(row[c]).decode() if row[c] else None) for c in _BLOB_COLS},
            }
    finally:
        conn.close()


def run_for_user(apply: bool, verbose: bool) -> dict:
    orphans = find_orphans()
    stats: Counter[str] = Counter()
    for o in orphans:
        twin = o["twin_bytes"]
        if twin is None:
            o["action"] = "rekey"
        elif o["bytes"] > twin:
            o["action"] = "replace-thinner-twin"
        else:
            o["action"] = "drop-redundant"
        stats[o["action"]] += 1
        if verbose:
            print(f"    {o['action']:<20} {o['bytes']:>6}b"
                  f" (twin {twin if twin is not None else '-'})  {o['title']}")

    if apply and orphans:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        undo = tenancy.user_data_dir() / f"repaired_orphan_archives_{stamp}.json"
        # Anything about to be *deleted* is snapshotted with its blobs, not just
        # its metadata: a re-key is reversible from the key alone, a delete is
        # not, and "the twin is at least as big" is not proof the bytes are the
        # same. Captures are the one copy of a page that may no longer exist.
        _snapshot_deleted_blobs(orphans)
        undo.write_text(json.dumps(orphans, indent=1))
        print(f"    undo snapshot: {undo} ({undo.stat().st_size // 1024} KB)")

        svc = main.starred_archive_service
        for o in orphans:
            if o["action"] == "replace-thinner-twin":
                # rekey_archive would delete our (better) source instead; clear
                # the thinner target first so the re-key lands.
                svc.delete_archive(o["dst_feed"], o["entry_id"])
            svc.rekey_archive(o["src_feed"], o["entry_id"], o["dst_feed"], o["entry_id"])

        # Refresh the link column: a re-keyed row otherwise still advertises the
        # dead domain, which is what surfaced this in the Saved view.
        conn = sqlite3.connect(str(tenancy.starred_archive_db_path()), timeout=30.0)
        try:
            fixed = 0
            for o in orphans:
                if o["action"] == "drop-redundant" or not o["fresh_link"]:
                    continue
                if o["fresh_link"] == o["stale_link"]:
                    continue
                fixed += conn.execute(
                    "UPDATE archived_entry SET link = ? WHERE feed_url = ? AND entry_id = ?",
                    (o["fresh_link"], o["dst_feed"], o["entry_id"]),
                ).rowcount
            conn.commit()
            stats["links_refreshed"] = fixed
        finally:
            conn.close()
    return dict(stats) | {"orphans": len(orphans)}


def main_cli() -> None:
    ap = argparse.ArgumentParser(description="Re-attach captures orphaned by a feed combine.")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--user", default=None)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    users = [args.user] if args.user else main._background_user_ids()
    print(f"repair orphaned archives — {'APPLY' if args.apply else 'DRY-RUN'} — users: {users}\n")
    for uid in users:
        print(f"[{uid}]")
        with tenancy.user_context(uid):
            s = run_for_user(args.apply, not args.quiet)
        print(f"  {s}\n")
    if not args.apply:
        print("Dry-run only — re-run with --apply.")


if __name__ == "__main__":
    main_cli()
