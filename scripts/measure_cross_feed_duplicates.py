"""Re-measure cross-feed article duplicates among ALL starred entries (not
just lectio:saved) -- the scan described in Plan.md "Cross-feed duplicate
scan". Last measured 2026-07-22 (65 groups, 87 extra copies) before this
session's other work; re-run before deciding whether the UI tier described
there is still worth building.

Read-only. Reuses the exact matching keys /saved/duplicates already uses
(main._saved_dup_groups, normalize_entry_link_for_dedupe,
normalize_entry_title_for_dedupe) so this measurement can't drift from what
the real scan would find if built the same way -- just applied to every
starred entry instead of only the lectio:saved feed, and classified by
whether a group's members span more than one feed_url.

Usage (inside the app container):
    uv run scripts/measure_cross_feed_duplicates.py [--user u_x] [--json out.json]
"""
from __future__ import annotations

import argparse
import html as html_module
import json
import os
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from services import tenancy  # noqa: E402

_BODY_SQL_CHARS = main._SAVED_DUP_BODY_SQL_CHARS
_BODY_HEAD_CHARS = main._SAVED_DUP_BODY_HEAD_CHARS


def _is_homepage_link(link: str) -> bool:
    """romhacking.net's feed uses the site homepage as every entry's link --
    a bare-domain/homepage link is not a real match signal, it's every post
    on the site looking identical. See Plan.md's guard note on this exact
    scan."""
    try:
        from urllib.parse import urlparse
        # romhacking.net's actual path is "//" (a feed-generator artifact,
        # base path + trailing slash concatenated) -- strip("/") == "" catches
        # that along with the plain "" and "/" cases a naive equality check
        # would miss.
        return urlparse(link).path.strip("/") == ""
    except ValueError:
        return True


def _load_records(uid: str) -> list[dict]:
    reader_db = str(tenancy.reader_db_path())
    meta_db = str(tenancy.meta_db_path())
    conn = sqlite3.connect(f"file:{reader_db}?mode=ro", uri=True, timeout=30.0)
    conn.execute("ATTACH DATABASE ? AS meta", (f"file:{meta_db}?mode=ro",))
    rows = conn.execute(
        "SELECT e.feed, e.id, e.link, e.title, e.published, e.read,"
        " substr(json_extract(e.content, '$[0].value'), 1, ?)"
        " FROM entries e JOIN meta.saved_entries se"
        "   ON se.feed_url = e.feed AND se.entry_id = e.id",
        (_BODY_SQL_CHARS,),
    ).fetchall()
    conn.close()

    host_aliases = main.get_dedupe_host_aliases()
    records: list[dict] = []
    for feed_url, entry_id, link, title, _published, _read, body_head in rows:
        link = str(link or entry_id)
        if _is_homepage_link(link):
            continue
        body = ""
        if body_head:
            body = main._SAFE_DEDUP_TAG_RE.sub(" ", body_head)
            body = main._SAFE_DEDUP_UNCLOSED_TAG_RE.sub("", body)
            body = html_module.unescape(body)
            body = " ".join(body.split())[:_BODY_HEAD_CHARS].lower()
        ntitle = main.normalize_entry_title_for_dedupe(title)
        records.append({
            "entry_id": str(entry_id),
            "feed_url": str(feed_url),
            "link": link,
            "title": str(title or ""),
            "_canon": main.normalize_entry_link_for_dedupe(link, host_aliases),
            "_slug": main._saved_dup_host_slug(link, host_aliases),
            "_ntitle": ntitle if len(ntitle.split()) >= main._SAFE_DEDUP_MIN_TITLE_WORDS else "",
            "_body": body if len(body) >= main._SAFE_DEDUP_MIN_BODY_CHARS else "",
        })
    return records


def _classify(group: list[dict]) -> str:
    feed_urls = {r["feed_url"] for r in group}
    if len(feed_urls) == 1:
        return "same_feed"
    if main.saved_articles_service.SAVED_FEED_URL in feed_urls:
        return "saved_vs_real"
    return "cross_feed"


def measure_for_user(uid: str) -> dict:
    records = _load_records(uid)
    if not records:
        return {"starred": 0, "groups": []}

    confirmed = main._saved_dup_groups(records, ("_canon", "_slug"))
    confirmed_member: dict[str, int] = {}
    for gi, group in enumerate(confirmed):
        for r in group:
            confirmed_member[r["entry_id"]] = gi

    possible = []
    for group in main._saved_dup_groups(records, ("_ntitle", "_body")):
        gids = {confirmed_member.get(r["entry_id"], -1 - i) for i, r in enumerate(group)}
        if len(gids) == 1 and next(iter(gids)) >= 0:
            continue
        possible.append(group)

    all_groups = confirmed + possible
    by_class = Counter(_classify(g) for g in all_groups)
    extra_copies = sum(len(g) - 1 for g in all_groups)
    biggest = sorted(all_groups, key=len, reverse=True)[:5]

    return {
        "starred": len(records),
        "groups_total": len(all_groups),
        "groups_confirmed": len(confirmed),
        "groups_possible": len(possible),
        "extra_copies": extra_copies,
        "by_class": dict(by_class),
        "biggest_groups": [
            {"size": len(g), "title": g[0]["title"], "feeds": sorted({r["feed_url"] for r in g})}
            for g in biggest
        ],
    }


def main_cli() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", default=None)
    ap.add_argument("--json")
    args = ap.parse_args()

    users = [args.user] if args.user else main._background_user_ids()
    results = {}
    for uid in users:
        with tenancy.user_context(uid):
            results[uid] = measure_for_user(uid)
        print(f"[{uid}]")
        for k, v in results[uid].items():
            if k != "biggest_groups":
                print(f"  {k}: {v}")
        for g in results[uid].get("biggest_groups", []):
            print(f"    size={g['size']} feeds={g['feeds']} title={g['title'][:70]!r}")
        print()

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(results, fh, indent=2)
        print(f"wrote {args.json}")


if __name__ == "__main__":
    main_cli()
