"""Blank the archived readability copy for entries whose recapture landed on a
sibling's boilerplate instead of the article.

`StarredArchiveService._archive_entry`'s readability re-extraction gained a
sibling-extraction guard (`extraction_matches_sibling`) on 2026-09-17, after
the 1MB+ recapture sweep run the day before demonstrated the gap: a redirect
target whose title happens to share one word with the original slug clears
guard 1 (slug/title mismatch) every time. guitarworld.com's retired
`/lessons/<slug>` URLs all now 301 to a "Lessons Coverage | Guitar World"
category page -- "guitar" is in nearly every slug on the feed. A scan the same
day found **1,524 entries across 230 feeds** already carrying a sibling's
extracted text this way, including commandlinefu.com and informit.com -- the
two sites `extraction_matches_sibling` was originally built for (see
`scripts/revert_boilerplate_refetches.py`), now recurring in this second path.

Unlike that script, there is nothing to *restore* here: `readability_html_zlib`
is derived fresh at capture time, not edited in place, so there is no
`entry_content_edits` snapshot of "the version before." The correct repair is
simply to blank it -- exactly what the now-fixed guard would have done had it
been in place during the sweep. `content_html`/`summary_html` (reader's own
copy, never touched by any of this) are unaffected either way; Reader View and
the e-ink `/read` view already fall back to them when the archive has no
readability copy (see docs/architecture/saved.md, "A guard-refused entry still
needs somewhere safe to fall").

**Detection is the same test the guard uses** (`sibling_extraction_entries`,
the bulk form living beside the live guard in services/starred_archive.py) --
a sweep and the guard can never disagree about what counts as boilerplate.

    uv run python scripts/clear_sibling_boilerplate_readability.py            # dry run
    uv run python scripts/clear_sibling_boilerplate_readability.py --feed <url>
    uv run python scripts/clear_sibling_boilerplate_readability.py --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from services import tenancy  # noqa: E402


def clear_for_user(user_id: str, apply: bool, only_feed: str | None) -> int:
    victims = main.starred_archive_service.sibling_extraction_entries(only_feed)
    feeds = {f for f, _ in victims}
    print(f"[{user_id}] {len(victims):,} entr(ies) share an extraction with a sibling across {len(feeds)} feed(s)", flush=True)
    if not victims:
        return 0
    for feed_url, entry_id in victims[:5]:
        print(f"    {feed_url} / {entry_id[:80]}", flush=True)
    if not apply:
        print("  dry run — re-run with --apply to blank", flush=True)
        return len(victims)

    cleared = 0
    with main.archive_conn() as conn:
        for feed_url, entry_id in victims:
            conn.execute(
                "UPDATE archived_entry SET readability_html_zlib = NULL WHERE feed_url = ? AND entry_id = ?",
                (feed_url, entry_id),
            )
            cleared += 1
    for feed_url, entry_id in victims:
        main.starred_archive_service.recompute_content_size_bytes(feed_url, entry_id)

    log = [{"feed_url": f, "entry_id": e} for f, e in victims]
    out = tenancy.meta_db_path().parent / f"cleared_sibling_boilerplate_readability_{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(log, indent=2))
    print(f"  cleared readability_html on {cleared:,} entr(ies). Log: {out}", flush=True)
    return cleared


def main_cli() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="blank (default: dry run)")
    ap.add_argument("--user", default=None, help="restrict to one user_id")
    ap.add_argument("--feed", default=None, help="restrict to one feed URL")
    args = ap.parse_args()

    for uid in [args.user] if args.user else main._background_user_ids():
        with tenancy.user_context(uid):
            clear_for_user(uid, args.apply, args.feed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
