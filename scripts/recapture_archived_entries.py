"""Force a specific starred/kept entry to be re-archived from scratch.

Two of this session's oversized-archive fixes (2026-09-12) are go-forward
only: the enclosure attachment-policy gate and the source_html image-scope
fix (see docs/architecture/saved.md, "Image harvesting scanned the whole
fetched page, not the article"). Neither shrinks an entry archived BEFORE the
fix landed — `enqueue_archive` is a no-op against an already-`complete` row
(see StarredArchiveService.enqueue_archive's ON CONFLICT clause), so simply
re-starring or re-fetching an already-archived entry does not re-run the
asset scan under the corrected rules.

This does what a "force recapture" button would: delete the archive row and
its now-orphaned assets (StarredArchiveService.delete_archive — the same
cascade the removal worker and a redundant-move both already use), then
enqueue it fresh (StarredArchiveService.enqueue_archive). The live archive
worker (polling every ARCHIVE_WORKER_POLL_INTERVAL_S, 5s) picks it up and
re-runs `_archive_entry` under whatever policy is live right now. The star
itself, its saved_at, tags and read state are untouched -- only the archived
capture is rebuilt.

Deliberately entry-at-a-time, not a full-library sweep: recapturing re-fetches
the source page over the network, and a blind pass over every archive built
before 2026-09-12 would be a much bigger, slower, more disruptive operation
than fixing the handful of entries actually reported as bloated. See Plan.md
for the larger "sweep everything" follow-up, not attempted here.

Usage (inside the app container):
    uv run scripts/recapture_archived_entries.py --user u_x FEED_URL ENTRY_ID [FEED_URL ENTRY_ID ...]
    uv run scripts/recapture_archived_entries.py --user u_x --apply FEED_URL ENTRY_ID
Dry-run by default: reports the current size and asset count, applies nothing.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from services import tenancy  # noqa: E402


def _pairs_from_args(args: list[str]) -> list[tuple[str, str]]:
    if len(args) % 2 != 0:
        raise SystemExit("Arguments must be FEED_URL ENTRY_ID pairs.")
    return [(args[i], args[i + 1]) for i in range(0, len(args), 2)]


def recapture(feed_url: str, entry_id: str, apply: bool) -> dict:
    with main.archive_conn() as conn:
        row = conn.execute(
            "SELECT status, content_size_bytes FROM archived_entry WHERE feed_url = ? AND entry_id = ?",
            (feed_url, entry_id),
        ).fetchone()
        link_count = conn.execute(
            "SELECT COUNT(*) FROM archived_asset_link WHERE feed_url = ? AND entry_id = ?",
            (feed_url, entry_id),
        ).fetchone()[0]
    if row is None:
        return {"ok": False, "error": "no archived_entry row for this (feed_url, entry_id)"}
    before = {"status": row["status"], "content_size_bytes": row["content_size_bytes"], "asset_links": link_count}
    if not apply:
        return {"ok": True, "before": before, "applied": False}
    if not main.starred_archive_service.delete_archive(feed_url, entry_id):
        return {"ok": False, "error": "delete_archive failed", "before": before}
    main.starred_archive_service.enqueue_archive(feed_url, entry_id)
    return {"ok": True, "before": before, "applied": True, "note": "re-enqueued -- picked up by the archive worker within ~5s"}


def main_cli() -> None:
    ap = argparse.ArgumentParser(description="Force a starred/kept entry to be re-archived from scratch.")
    ap.add_argument("--user", required=True, help="user_id to act as.")
    ap.add_argument("--apply", action="store_true", help="Actually delete + re-enqueue (default: dry-run report).")
    ap.add_argument("pairs", nargs="+", help="FEED_URL ENTRY_ID [FEED_URL ENTRY_ID ...]")
    args = ap.parse_args()

    pairs = _pairs_from_args(args.pairs)
    mode = "APPLY" if args.apply else "DRY-RUN (read-only)"
    print(f"Recapture — {mode} — user: {args.user}\n")

    with tenancy.user_context(args.user):
        for feed_url, entry_id in pairs:
            result = recapture(feed_url, entry_id, args.apply)
            print(f"{feed_url} / {entry_id}\n  {result}\n")

    if not args.apply:
        print("Dry-run only — no changes made. Re-run with --apply to recapture.")


if __name__ == "__main__":
    main_cli()
