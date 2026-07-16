"""Remove stale duplicate saved_entries rows left by feed migrations.

A feed-URL migration (e.g. feedburner -> direct) could leave the same article
saved twice: once under the surviving feed (reader entry exists) and once under
the old feed whose reader entries were later deleted. The dead twin still counts
in saved badges and surfaces as a phantom archive-orphan card in star views.

A row is deleted ONLY when both hold:
  * its (feed_url, entry_id) has NO entry in the reader DB, and
  * another saved row with the SAME entry_id DOES have a live reader entry.
Genuine archive-only orphans (no live twin anywhere) are never touched.

The dead twin's starred-archive capture is enqueued for removal as well (the
live twin has its own capture; the worker garbage-collects shared assets).

Usage (inside the container, per user):
    PYTHONPATH=/app /app/.venv/bin/python scripts/cleanup_duplicate_saved_entries.py [--apply] [--user USER_ID]
Dry-run by default.
"""
from __future__ import annotations

import argparse
import sys

import main  # noqa: E402
from services import tenancy


def cleanup(user_id: str, apply: bool) -> tuple[int, int]:
    checked = 0
    removed = 0
    with tenancy.user_context(user_id):
        with main.get_meta_connection() as conn:
            rows = conn.execute(
                "SELECT entry_id FROM saved_entries GROUP BY entry_id "
                "HAVING COUNT(DISTINCT feed_url) > 1"
            ).fetchall()
            dup_ids = [str(r["entry_id"]) for r in rows]
        print(f"[{user_id}] entry ids saved under multiple feeds: {len(dup_ids)}")

        with main.get_reader() as reader:
            for eid in dup_ids:
                with main.get_meta_connection() as conn:
                    feeds = [
                        str(r["feed_url"])
                        for r in conn.execute(
                            "SELECT feed_url FROM saved_entries WHERE entry_id = ?", (eid,)
                        )
                    ]
                live = [f for f in feeds if reader.get_entry((f, eid), None) is not None]
                dead = [f for f in feeds if f not in live]
                checked += 1
                if not live or not dead:
                    continue  # keep genuine orphans / nothing to clean
                for f in dead:
                    removed += 1
                    if apply:
                        with main.get_meta_connection() as conn:
                            conn.execute(
                                "DELETE FROM saved_entries WHERE feed_url = ? AND entry_id = ?",
                                (f, eid),
                            )
                            conn.commit()
                        try:
                            main.starred_archive_service.enqueue_removal(f, eid)
                        except Exception as exc:  # noqa: BLE001
                            print(f"  archive removal enqueue failed for {f} / {eid}: {exc}")
                    else:
                        print(f"  would remove: {f}  |  {eid[:70]}")
    return checked, removed


def resolve_users(explicit: str | None) -> list[str]:
    if explicit:
        return [explicit]
    users = main.user_store.list_users()
    return [u["user_id"] if isinstance(u, dict) else getattr(u, "user_id", str(u)) for u in users]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    ap.add_argument("--user", default=None, help="single user id (default: all users)")
    args = ap.parse_args()
    total_removed = 0
    for uid in resolve_users(args.user):
        _, removed = cleanup(uid, args.apply)
        total_removed += removed
        print(f"[{uid}] {'removed' if args.apply else 'would remove'}: {removed}")
    if not args.apply and total_removed:
        print("\nDry run only — re-run with --apply to delete.")
    sys.exit(0)
