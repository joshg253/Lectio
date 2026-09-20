"""Read-only reachability probe for the "617 complete archives have no content
at all" Plan.md item: archived_entry rows marked status='complete' with every
content column empty (source_html_zlib, readability_html_zlib,
content_html_zlib all NULL/zero-length) -- not just a missing size, so
backfill_archived_entry_sizes.py (which only targets NULL) doesn't catch them.

Confirmed 2026-09-19: the cause is a silent capture failure, not genuinely
link-less posts. _archive_entry's source fetch (_fetch_text_with_url) swallows
every exception and returns None on ANY failure -- a 404, a 403, a TLS error,
anything -- logged at DEBUG only, no `error` recorded. A permanently dead link
and a merely transient failure look identical in the DB (status='complete',
error=NULL, all content empty), and nothing ever retries either one.

This script tells the two apart with a live fetch, mirroring
find_redirecting_feeds.py's shape: read-only, paced, honest UA. Writes nothing;
reports counts and (with --json) a candidate list of (feed_url, entry_id)
pairs that are reachable right now, for scripts/recapture_archived_entries.py
to pick up -- this script deliberately does not recapture anything itself,
same reasoning recapture_archived_entries.py already gives for staying
entry-at-a-time rather than a blind bulk mode.

Usage:
    docker compose exec lectio uv run scripts/probe_empty_archives.py --user u_x
    docker compose exec lectio uv run scripts/probe_empty_archives.py --user u_x --json out.json
    docker compose exec lectio uv run scripts/probe_empty_archives.py --user u_x --limit 50
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from services import tenancy, url_guard  # noqa: E402

_DELAY_SECONDS = 1.0


def _find_candidates(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT feed_url, entry_id, title, link, feed_title
        FROM archived_entry
        WHERE status = 'complete'
          AND (source_html_zlib IS NULL OR LENGTH(source_html_zlib) = 0)
          AND (readability_html_zlib IS NULL OR LENGTH(readability_html_zlib) = 0)
          AND (content_html_zlib IS NULL OR LENGTH(content_html_zlib) = 0)
        """
    ).fetchall()
    return [dict(r) for r in rows]


def _probe(url: str) -> dict:
    if not url:
        return {"verdict": "no-link"}
    try:
        with url_guard.build_client(timeout=15, headers={"User-Agent": "Mozilla/5.0 (compatible; Lectio/1.0)"}) as client:
            resp = url_guard.safe_get(client, url)
    except Exception as exc:  # noqa: BLE001 -- any fetch failure is a verdict, not a crash
        return {"verdict": "unreachable", "detail": f"{type(exc).__name__}: {exc}"[:200]}
    if resp.status_code >= 400:
        return {"verdict": "dead", "status": resp.status_code}
    body_len = len(resp.text or "")
    if body_len < 500:
        return {"verdict": "thin", "status": resp.status_code, "bytes": body_len}
    return {"verdict": "reachable", "status": resp.status_code, "bytes": body_len}


def run(uid: str, limit: int | None, json_path: str | None) -> None:
    with tenancy.user_context(uid):
        with main.archive_conn() as conn:
            candidates = _find_candidates(conn)
    if limit:
        candidates = candidates[:limit]
    print(f"[{uid}] {len(candidates)} empty-content complete archive(s) to probe")

    results: list[dict] = []
    counts: dict[str, int] = {}
    for i, row in enumerate(candidates, 1):
        verdict = _probe(row.get("link") or row["entry_id"])
        counts[verdict["verdict"]] = counts.get(verdict["verdict"], 0) + 1
        results.append({**row, **verdict})
        if i % 25 == 0 or i == len(candidates):
            print(f"  [{i}/{len(candidates)}] {counts}", flush=True)
        time.sleep(_DELAY_SECONDS)

    print(f"\n[{uid}] final: {counts}")
    reachable = [r for r in results if r["verdict"] in ("reachable", "thin")]
    print(f"[{uid}] {len(reachable)} candidate(s) worth a recapture attempt")

    if json_path:
        with open(json_path, "w") as fh:
            json.dump(results, fh, indent=2)
        print(f"wrote {json_path}")


def main_cli() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", required=True, help="user_id to operate on")
    ap.add_argument("--limit", type=int, default=None, help="cap the number of entries probed")
    ap.add_argument("--json", dest="json_path", default=None, help="write full per-entry results to this path")
    args = ap.parse_args()
    run(args.user, args.limit, args.json_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
