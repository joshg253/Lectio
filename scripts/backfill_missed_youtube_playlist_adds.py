"""Backfill videos the youtube_playlist automation should have added but missed.

_apply_youtube_playlist_rules (main.py) only ever looks at entries with
entry.added >= a persisted watermark. PR #261 (2026-08-29) fixed the watermark
logic going forward -- a slow/batched refresh can no longer open a permanent
gap -- but it can't recover entries whose `added` timestamp had already fallen
behind the watermark before the fix shipped. This walks each enabled
youtube_playlist rule's full scope back to --since and adds anything still
missing, reusing the exact same matching / dedup-guard / quota-cap logic as
the live automation (same function, just called with a wide-open cutoff and
the rule's whole scope instead of one refresh tick's feeds).

"Missing" is decided against each rule's LIVE playlist contents (a read-only
playlistItems.list call), not the local youtube_playlist_added table. That
table's dedup key is (scope, scope_id, keyword) -- a text snapshot of the
rule -- so any edit to a rule's scope (adding/removing a feed) orphans its
prior dedup history and makes already-added videos look unclaimed again.
Trusting it here would re-submit videos already in the playlist, and
playlistItems.insert is not idempotent -- that would create duplicates.
Before applying, already-present videos are pre-claimed in the local table so
the shared apply function skips them instead of re-adding.

Usage (inside the app container so LECTIO_DATA_DIR=/data resolves):
    # READ-ONLY report (default) -- lists what's missing; makes read-only API
    # calls to check live playlist contents, no writes:
    uv run scripts/backfill_missed_youtube_playlist_adds.py
    # WRITES (after review) -- adds to the real YouTube playlists, costs quota:
    uv run scripts/backfill_missed_youtube_playlist_adds.py --apply
    # restrict user / widen or narrow the window:
    uv run scripts/backfill_missed_youtube_playlist_adds.py --apply --user u_x --since 2026-08-01

Respects the same _YT_PLAYLIST_AUTO_PER_RUN_CAP (25 adds/call) as the live
automation -- if a run reports exactly that many added, some may still be
missing; re-run to continue.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from services import tenancy  # noqa: E402


def _rule_scope_feed_urls(conn: sqlite3.Connection, rule: dict) -> set[str]:
    scope = str(rule.get("scope", ""))
    scope_id = str(rule.get("scope_id") or "")
    if scope == "feed":
        return {scope_id} if scope_id else set()
    if scope == "feeds":
        return set(main.parse_feeds_scope_id(scope_id))
    if scope == "folder" and scope_id.isdigit():
        return main.get_folder_feed_urls(conn, int(scope_id))
    if scope == "global":
        with main.get_reader() as reader:
            return {str(f.url) for f in reader.get_feeds()}
    return set()


def _scan_scope(rule: dict, feed_urls: set[str], cutoff: datetime) -> Iterator[tuple[object, str]]:
    """Yield (entry, video_id) for every scope candidate that passes the
    rule's keyword/shorts/duration filters -- the same predicate
    _apply_youtube_playlist_rules uses, without touching the dedup table or
    calling the playlist-insert API."""
    keyword = str(rule.get("keyword", ""))
    is_regex = bool(rule.get("is_regex"))
    search_in = str(rule.get("search_in") or "title")
    include_shorts = bool(rule.get("yt_include_shorts"))
    min_secs = max(0, int(rule.get("yt_min_minutes") or 0)) * 60
    max_secs = max(0, int(rule.get("yt_max_minutes") or 0)) * 60

    with main.get_reader() as reader:
        for feed_url in feed_urls:
            for entry in reader.get_entries(feed=feed_url):
                added = getattr(entry, "added", None)
                if not added or added < cutoff:
                    continue
                if keyword and not main._entry_matches_rule(entry, keyword, is_regex, search_in):
                    continue
                if not include_shorts and main._is_youtube_short(entry):
                    continue
                link = str(entry.link or "")
                body = "".join((c.value or "") for c in (entry.content or []))
                body += str(entry.summary or "")
                for vid in main.youtube_embeds.video_ids_in_text(link, body):
                    if min_secs or max_secs:
                        dur = main.youtube_duration_service.get_cached_duration(vid)[0]
                        if dur is None or (min_secs and dur < min_secs) or (max_secs and dur > max_secs):
                            continue
                    yield entry, vid


def _playlist_video_ids(token: str, playlist_id: str) -> set[str]:
    try:
        return main.youtube_oauth_service.list_playlist_video_ids(token, playlist_id)
    except Exception as exc:  # noqa: BLE001
        print(f"      [warn] could not read live playlist {playlist_id}: {exc}")
        return set()


def _missing_against_live(rule: dict, feed_urls: set[str], cutoff: datetime, live_ids: set[str]) -> list[dict]:
    found: list[dict] = []
    seen: set[str] = set()
    for entry, vid in _scan_scope(rule, feed_urls, cutoff):
        if vid in live_ids or vid in seen:
            continue
        seen.add(vid)
        found.append({
            "feed": str(getattr(entry, "feed_url", "") or ""),
            "title": str(getattr(entry, "title", "") or ""),
            "video_id": vid,
        })
    return found


def _seed_already_present(rule: dict, feed_urls: set[str], cutoff: datetime, live_ids: set[str]) -> int:
    """Pre-claim the dedup guard for videos genuinely already in the live
    playlist, so _apply_youtube_playlist_rules's own INSERT OR IGNORE skips
    them instead of re-submitting a duplicate."""
    scope = str(rule.get("scope", ""))
    scope_id = str(rule.get("scope_id") or "")
    keyword = str(rule.get("keyword", ""))
    now_str = datetime.now().isoformat()
    seeded = 0
    with main.get_meta_connection() as conn:
        for entry, vid in _scan_scope(rule, feed_urls, cutoff):
            if vid not in live_ids:
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO youtube_playlist_added"
                " (scope, scope_id, keyword, entry_id, video_id, added_at) VALUES (?,?,?,?,?,?)",
                (scope, scope_id, keyword, str(getattr(entry, "id", "")), vid, now_str),
            )
            seeded += cur.rowcount
    return seeded


def run_for_user(uid: str, args: argparse.Namespace, cutoff: datetime) -> None:
    with main.get_meta_connection() as conn:
        all_rules = main.get_highlight_keywords(conn)
        yt_rules = [
            r for r in all_rules
            if r.get("enabled") and r.get("type") == "youtube_playlist" and r.get("yt_playlist_id")
        ]
        if not yt_rules:
            print(f"[{uid}] no enabled youtube_playlist rules")
            return
        folder_feed_map = {
            int(r["scope_id"]): main.get_folder_feed_urls(conn, int(r["scope_id"]))
            for r in yt_rules if r["scope"] == "folder" and str(r.get("scope_id", "")).isdigit()
        }
        scope_feeds = [_rule_scope_feed_urls(conn, r) for r in yt_rules]

    all_feed_urls: set[str] = set().union(*scope_feeds) if scope_feeds else set()
    print(f"[{uid}] {len(yt_rules)} rule(s), scanning {len(all_feed_urls)} feed(s) since {args.since}")

    token = main.get_youtube_oauth_token()
    if not token:
        print(f"[{uid}] no YouTube token -- reconnect needed, skipping")
        return

    for rule, feeds in zip(yt_rules, scope_feeds, strict=True):
        label = rule.get("yt_playlist_title") or rule.get("yt_playlist_id")
        live_ids = _playlist_video_ids(token, str(rule.get("yt_playlist_id") or ""))
        missing = _missing_against_live(rule, feeds, cutoff, live_ids)
        print(f"  -> {label}: {len(missing)} missing (playlist currently has {len(live_ids)} video(s))")
        for m in missing[:20]:
            print(f"      {m['video_id']}  {m['title']}")
        if len(missing) > 20:
            print(f"      ... and {len(missing) - 20} more")
        if args.apply:
            seeded = _seed_already_present(rule, feeds, cutoff, live_ids)
            if seeded:
                print(f"      (pre-claimed {seeded} already-in-playlist video(s) so they won't be re-added)")

    if not args.apply:
        return

    added = main._apply_youtube_playlist_rules(
        all_feed_urls, cutoff, yt_rules, folder_feed_map, token, trigger="backfill",
    )
    print(f"[{uid}] added {added} video(s)")
    if added >= main._YT_PLAYLIST_AUTO_PER_RUN_CAP:
        print(f"[{uid}] hit the per-run cap ({main._YT_PLAYLIST_AUTO_PER_RUN_CAP}) -- re-run to continue")


def main_cli() -> None:
    ap = argparse.ArgumentParser(description="Backfill videos the youtube_playlist automation missed.")
    ap.add_argument("--apply", action="store_true", help="Perform writes/API calls (default: dry-run report only).")
    ap.add_argument("--user", default=None, help="Restrict to one user_id (default: all enabled users).")
    ap.add_argument(
        "--since", default=(datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d"),
        help="Only consider entries added on/after this date, YYYY-MM-DD (default: 60 days back).",
    )
    args = ap.parse_args()
    cutoff = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)

    users = [args.user] if args.user else main._background_user_ids()
    mode = "APPLY (writing + calling the YouTube API)" if args.apply else "DRY-RUN (read-only, checks live playlist contents)"
    print(f"youtube-playlist backfill -- {mode} -- since {args.since} -- users: {users}\n")

    for uid in users:
        with tenancy.user_context(uid):
            run_for_user(uid, args, cutoff)

    if not args.apply:
        print("\nDry-run only -- no writes made. Re-run with --apply to add these.")


if __name__ == "__main__":
    main_cli()
