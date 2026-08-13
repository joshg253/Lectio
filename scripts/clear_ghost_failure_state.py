"""Drop ``feed_failure_state`` rows for feeds that no longer exist.

``purge_orphaned_feed`` never cleared this table, so every feed removed over the
app's life stayed on record as failing. The 404 sweep on 2026-08-11/12 made that
visible: feeds unsubscribed *because* they were dead went on being counted by
Failing Feeds and by the "dead — needs replacement" triage, with no subscription
left to fix or remove. 560 such rows on the live library.

The code leak is fixed in ``purge_orphaned_feed``; this clears the backlog it
left behind. Nothing here is recoverable state — a failure record is rebuilt on
the next fetch, and a feed with no reader row has no next fetch.

**Ghost is defined against reader, not against folders.** A feed can legitimately
have no folder row and still exist (unsubscribe-with-keep leaves it reachable
through the Kept view), so keying on `folder_feeds` would delete the failure
record of feeds that are still real.

Usage:
    LECTIO_DATA_DIR=/data uv run scripts/clear_ghost_failure_state.py \
        --user <user_id> [--apply]

Defaults to a dry run; --apply performs the deletes.
"""
from __future__ import annotations

import argparse
import sys

sys.path.insert(0, "/app")

import main  # noqa: E402


def run(apply: bool) -> int:
    with main.get_reader() as reader:
        live = {str(f.url) for f in reader.get_feeds()}

    with main.get_meta_connection() as conn:
        rows = conn.execute(
            "SELECT feed_url, consecutive_failures, last_error FROM feed_failure_state"
        ).fetchall()

    ghosts = [r for r in rows if str(r["feed_url"]) not in live]
    print(f"live feeds:            {len(live)}")
    print(f"feed_failure_state:    {len(rows)}")
    print(f"ghost rows:            {len(ghosts)}")
    for r in ghosts[:15]:
        err = str(r["last_error"] or "")[:60]
        print(f"   {r['consecutive_failures']:>4} fails  {r['feed_url']}  ({err})")
    if len(ghosts) > 15:
        print(f"   … and {len(ghosts) - 15} more")

    if not apply:
        print("\nDRY RUN — pass --apply to delete.")
        return 0

    with main.get_meta_connection() as conn:
        conn.executemany(
            "DELETE FROM feed_failure_state WHERE feed_url = ?",
            [(str(r["feed_url"]),) for r in ghosts],
        )
        conn.commit()
    print(f"\ndeleted {len(ghosts)} ghost row(s).")
    return len(ghosts)


def main_cli() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    result: dict[str, int] = {}

    def _go() -> None:
        result["n"] = run(args.apply)

    main._run_in_user_context(args.user, _go)


if __name__ == "__main__":
    main_cli()
