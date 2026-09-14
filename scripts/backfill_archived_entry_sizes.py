"""One-time backfill: compute content_size_bytes for archived_entry rows that
predate the "sort Saved/Kept by item size" feature (2026-08-24, see
docs/architecture/saved.md "Saved/Kept item size: maintained, not computed
live"). That feature is deliberately go-forward only -- content_size_bytes is
computed once, at archive-completion time, and nothing backfills existing
archives. On the live library this left content_size_bytes NULL for ~98% of
completed archives (373 of 18,664), so "Biggest first" mostly sorted ties --
reported live 2026-09-11 as "sorting Inbox by Biggest First doesn't seem to
work."

This does the exact same computation StarredArchiveService._archive_entry
does at capture time -- len() of the three already-stored compressed blobs
plus the summed byte_size of every linked archived_asset -- against content
that is already fully captured (status='complete'). No re-fetch, no network
call, no decompression: just reading stored blob/asset lengths and writing
one column.

Usage (inside the app container):
    uv run scripts/backfill_archived_entry_sizes.py              # dry-run
    uv run scripts/backfill_archived_entry_sizes.py --apply
    uv run scripts/backfill_archived_entry_sizes.py --apply --user u_x --limit 500
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from services import tenancy  # noqa: E402

_CHUNK_SIZE = 500  # one transaction per chunk, not one giant transaction for
# the whole backlog -- the live archive service may need to
# write concurrently while this runs.


def _candidates(limit: int) -> list[tuple[str, str, bytes | None, bytes | None, bytes | None]]:
    """(feed_url, entry_id, source_html_zlib, readability_html_zlib, content_html_zlib)
    for completed archives with no size recorded yet."""
    with main.archive_conn() as conn:
        query = (
            "SELECT feed_url, entry_id, source_html_zlib, readability_html_zlib, content_html_zlib"
            " FROM archived_entry WHERE status = 'complete' AND content_size_bytes IS NULL"
            " ORDER BY feed_url, entry_id"
        )
        if limit:
            query += f" LIMIT {int(limit)}"
        rows = conn.execute(query).fetchall()
    return [(r["feed_url"], r["entry_id"], r["source_html_zlib"], r["readability_html_zlib"], r["content_html_zlib"]) for r in rows]


def _asset_totals(conn, pairs: list[tuple[str, str]]) -> dict[tuple[str, str], int]:
    """Summed archived_asset.byte_size per (feed_url, entry_id) in *pairs*, one
    query for the whole chunk rather than one per entry."""
    if not pairs:
        return {}
    # DISTINCT on (feed_url, entry_id, asset_hash) before summing: the same
    # image is routinely discovered at more than one URL for the same entry
    # (a raw CDN download link and a display-CDN mirror of the same file),
    # giving it two archived_asset_link rows for one already-deduped asset.
    # Grouping straight off the join would sum that asset's bytes once per
    # link row instead of once per distinct asset -- see the matching fix in
    # StarredArchiveService._archive_entry.
    placeholders = ",".join("(?,?)" for _ in pairs)
    params = [v for pair in pairs for v in pair]
    rows = conn.execute(
        f"SELECT feed_url, entry_id, COALESCE(SUM(byte_size), 0) AS total FROM ("
        f"  SELECT DISTINCT l.feed_url, l.entry_id, a.asset_hash, a.byte_size"
        f"  FROM archived_asset_link l JOIN archived_asset a ON a.asset_hash = l.asset_hash"
        f"  WHERE (l.feed_url, l.entry_id) IN ({placeholders})"
        f") GROUP BY feed_url, entry_id",
        params,
    ).fetchall()
    return {(r["feed_url"], r["entry_id"]): int(r["total"]) for r in rows}


def backfill_for_user(uid: str, apply: bool, limit: int) -> dict:
    candidates = _candidates(limit)
    if not apply:
        return {"candidates": len(candidates)}

    updated = 0
    for i in range(0, len(candidates), _CHUNK_SIZE):
        chunk = candidates[i : i + _CHUNK_SIZE]
        pairs = [(fu, eid) for fu, eid, *_ in chunk]
        with main.archive_conn() as conn:
            asset_totals = _asset_totals(conn, pairs)
            rows_to_update = []
            for feed_url, entry_id, source_blob, readability_blob, content_blob in chunk:
                size = (
                    len(source_blob or b"")
                    + len(readability_blob or b"")
                    + len(content_blob or b"")
                    + asset_totals.get((feed_url, entry_id), 0)
                )
                rows_to_update.append((size, feed_url, entry_id))
            conn.executemany(
                "UPDATE archived_entry SET content_size_bytes = ? WHERE feed_url = ? AND entry_id = ?",
                rows_to_update,
            )
        updated += len(chunk)
        print(f"  [{uid}] {updated}/{len(candidates)}", flush=True)
    return {"candidates": len(candidates), "updated": updated}


def main_cli() -> None:
    ap = argparse.ArgumentParser(description="Backfill content_size_bytes for existing completed archives.")
    ap.add_argument("--apply", action="store_true", help="Actually write sizes (default: dry-run report).")
    ap.add_argument("--user", default=None, help="Restrict to one user_id.")
    ap.add_argument("--limit", type=int, default=0, help="Cap the number of entries processed (0 = no cap).")
    args = ap.parse_args()

    users = [args.user] if args.user else main._background_user_ids()
    mode = "APPLY" if args.apply else "DRY-RUN (read-only)"
    print(f"Archived-entry size backfill — {mode} — users: {users}\n")

    for uid in users:
        with tenancy.user_context(uid):
            result = backfill_for_user(uid, args.apply, args.limit)
        print(f"[{uid}] {result}")

    if not args.apply:
        print("\nDry-run only — no changes made. Re-run with --apply to write sizes.")


if __name__ == "__main__":
    main_cli()
