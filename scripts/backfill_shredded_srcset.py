"""Re-fetch entries whose stored srcset was shredded by the comma-split bug
fixed 2026-09-18 in services/html_sanitize.py (resolve_relative_urls).

A CDN URL that embeds its own comma-separated transform params ahead of the
real path -- Substack and Cloudinary both do this:
    .../fetch/$s_!x!,w_424,c_limit,f_auto,q_auto:good,fl_progressive:steep/https%3A%2F%2F...jpeg
-- used to get cut at its first internal comma by a naive `raw.split(",")`,
and the leftover fragment (`w_424`) got resolved as a relative path against
the entry's own page, producing a same-host garbage URL
(`https://<entry-host>/<dir>/w_424`) that 404s. Every candidate in a shredded
srcset is garbage this way, and a browser prefers a matching srcset candidate
over `src` by the HTML spec -- so the image breaks even though `src` itself
(rewritten correctly for a kept entry) is fine. Reported live as "lead image
broken" on kriscox.substack.com; the code fix stops new entries from being
corrupted, this repairs the ones already written.

**Detection** looks for a same-host URL fragment shaped like a Cloudinary/
Substack transform token (w_123, c_limit, f_auto, q_auto, fl_progressive, …)
sitting where a real path segment should be -- the same signature regardless
of which of the 55 affected feeds it's on.

**Repair is a re-fetch**, not a text patch: the original comma-containing URL
is unrecoverable once shredded (the split already destroyed it), so the only
way back is `refresh_captured_article` against the live page -- the exact
mechanism the "Refetch content" button uses, same slug/sibling guards, same
pacing. An entry the guard refuses (parked page, moved, boilerplate) is
reported as `refused`, not silently skipped or re-damaged.

    uv run python scripts/backfill_shredded_srcset.py             # dry run
    uv run python scripts/backfill_shredded_srcset.py --feed <url>
    uv run python scripts/backfill_shredded_srcset.py --limit 20 --apply
    uv run python scripts/backfill_shredded_srcset.py --apply
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from services import saved_articles, tenancy  # noqa: E402

_TRANSFORM_TOKEN = r"(?:w_\d+|h_\d+|c_(?:limit|fill|fit|crop|scale|thumb)|f_auto|q_auto|fl_progressive|dpr_\d)"


def _is_shredded(content: str, own_host: str) -> bool:
    if not content or not own_host:
        return False
    pattern = rf"https?://{re.escape(own_host)}/[\w%\-]+/{_TRANSFORM_TOKEN}\b"
    return bool(re.search(pattern, content))


def find_candidates(only_feed: str | None) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    with main.get_reader() as reader:
        entries = reader.get_entries(feed=only_feed) if only_feed else reader.get_entries()
        for entry in entries:
            link = str(getattr(entry, "link", "") or "")
            if not link:
                continue
            host = urlparse(link).netloc
            content = entry.get_content(prefer_summary=False) if hasattr(entry, "get_content") else None
            body = content.value if content and content.value else ""
            summary = str(getattr(entry, "summary", "") or "")
            if _is_shredded(body, host) or _is_shredded(summary, host):
                out.append((str(entry.feed_url), str(entry.id)))
    return out


def run(uid: str, only_feed: str | None, apply: bool, limit: int | None) -> None:
    candidates = find_candidates(only_feed)
    feeds = {f for f, _ in candidates}
    print(f"[{uid}] {len(candidates):,} entr(ies) with a shredded srcset across {len(feeds)} feed(s)", flush=True)
    if not candidates:
        return
    if limit:
        candidates = candidates[:limit]
    if not apply:
        for feed_url, entry_id in candidates[:10]:
            print(f"    {feed_url} / {entry_id[:80]}", flush=True)
        print("  dry run — re-run with --apply to refetch", flush=True)
        return

    def extract(url: str):
        return main.fetch_readability_article(url, max_tier="flaresolverr")

    reader = main.get_reader()
    fixed = refused = failed = 0
    for i, (feed_url, entry_id) in enumerate(candidates, 1):
        with main.get_meta_connection() as conn:
            try:
                result = saved_articles.refresh_captured_article(
                    reader,
                    conn,
                    feed_url,
                    entry_id,
                    extract=extract,
                    enqueue_archive=main.starred_archive_service.enqueue_archive,
                    is_boilerplate_extraction=main.starred_archive_service.extraction_matches_sibling,
                )
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "error": str(exc)}
        if result.get("ok"):
            fixed += 1
        elif result.get("mismatch") or result.get("boilerplate"):
            refused += 1
        else:
            failed += 1
        if i % 25 == 0 or i == len(candidates):
            print(f"  [{uid}] {i}/{len(candidates)} fixed={fixed} refused={refused} failed={failed}", flush=True)
        time.sleep(0.5)
    print(f"[{uid}] done: fixed={fixed} refused={refused} failed={failed}", flush=True)


def main_cli() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="refetch (default: dry run)")
    ap.add_argument("--user", default=None, help="restrict to one user_id")
    ap.add_argument("--feed", default=None, help="restrict to one feed URL")
    ap.add_argument("--limit", type=int, default=None, help="cap the number of entries processed")
    args = ap.parse_args()

    for uid in [args.user] if args.user else main._background_user_ids():
        with tenancy.user_context(uid):
            run(uid, args.feed, args.apply, args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
