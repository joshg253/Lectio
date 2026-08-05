"""Capture attachments for posts that were already saved.

Attachment capture runs during `_archive_entry`, so it only ever applied to
posts saved *after* the feature existed: `enqueue_archive` keeps an existing row
at ``status='complete'`` and the worker only picks up ``pending``, so
re-starring an old post does not re-capture it either.

**No page is re-fetched.** The archive already stores each post's HTML, so the
links are found offline and only the FILES themselves are requested — which is
both faster and the polite way round. Enclosures come from the reader entry,
also already stored.

Same rules as the live path, deliberately sharing the code rather than
reimplementing it (`main.attachment_links_in_html`):

- body links only for feeds with an extension list configured, page types never;
- enclosures unconditionally, since an ``<enclosure>`` is the publisher
  declaring the file belongs to the post — minus audio (large, streams fine)
  and images (already captured as images);
- 25 MB per file.

    uv run python scripts/backfill_attachments.py                     # dry run
    uv run python scripts/backfill_attachments.py --feed <url>
    uv run python scripts/backfill_attachments.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import zlib
from collections import defaultdict
from datetime import datetime
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from services import starred_archive as starred_archive_service  # noqa: E402
from services import tenancy  # noqa: E402

_PACE_SECONDS = 0.5
_HOST_PACE_SECONDS = 1.5
_HOST_FAILURE_LIMIT = 3


def _stored_html(row) -> str:
    """The best stored copy of the post: the source page, else the feed content."""
    for key in ("source_html_zlib", "content_html_zlib", "readability_html_zlib"):
        blob = row[key]
        if blob:
            try:
                return zlib.decompress(blob).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                continue
    return ""


def _enclosure_urls(feed_url: str, entry_id: str) -> list[str]:
    """Non-audio, non-image enclosures, from the reader entry."""
    try:
        with main.get_reader() as reader:
            entry = reader.get_entry((feed_url, entry_id), None)
    except Exception:  # noqa: BLE001
        return []
    if entry is None:
        return []
    out: list[str] = []
    for enc in (getattr(entry, "enclosures", None) or []):
        url = str(getattr(enc, "href", None) or getattr(enc, "url", None) or "").strip()
        if not url:
            continue
        enc_type = str(getattr(enc, "type", None) or "").lower()
        if enc_type.startswith(("audio/", "image/")):
            continue
        if main._url_has_image_ext(url) or main._url_has_audio_ext(url):
            continue
        out.append(url)
    return out


def backfill_for_user(user_id: str, apply: bool, only_feed: str | None) -> int:
    with main.get_starred_archive_connection() as conn:
        rows = conn.execute(
            "SELECT feed_url, entry_id, source_html_zlib, content_html_zlib,"
            "       readability_html_zlib"
            "  FROM archived_entry WHERE status = 'complete'"
        ).fetchall()
    if only_feed:
        rows = [r for r in rows if str(r["feed_url"]) == only_feed]
    print(f"[{user_id}] {len(rows):,} saved post(s) to check", flush=True)

    exts_by_feed: dict[str, list[str]] = {}
    wanted: list[tuple[str, str, str]] = []   # (feed_url, entry_id, file_url)
    for row in rows:
        feed_url, entry_id = str(row["feed_url"]), str(row["entry_id"])
        if feed_url not in exts_by_feed:
            exts_by_feed[feed_url] = main.get_feed_attachment_exts(feed_url)
        urls: list[str] = list(_enclosure_urls(feed_url, entry_id))
        exts = exts_by_feed[feed_url]
        if exts:
            html_text = _stored_html(row)
            if html_text:
                urls.extend(main.attachment_links_in_html(html_text, entry_id, exts))
        for url in dict.fromkeys(urls):
            wanted.append((feed_url, entry_id, url))

    # Skip anything already linked to this entry — re-running must be free.
    todo: list[tuple[str, str, str]] = []
    with main.get_starred_archive_connection() as conn:
        for feed_url, entry_id, url in wanted:
            got = conn.execute(
                "SELECT 1 FROM archived_asset_link"
                " WHERE feed_url = ? AND entry_id = ? AND source_url = ?",
                (feed_url, entry_id, url),
            ).fetchone()
            if not got:
                todo.append((feed_url, entry_id, url))

    print(f"  {len(wanted):,} attachment(s) referenced, {len(todo):,} not yet stored",
          flush=True)
    for _f, _e, url in todo[:6]:
        print(f"    {url[:96]}", flush=True)
    if not todo:
        return 0
    if not apply:
        print("  dry run — re-run with --apply to fetch and store", flush=True)
        return len(todo)

    stored: list[dict] = []
    host_fail: dict[str, int] = defaultdict(int)
    host_last: dict[str, float] = {}
    last_request = 0.0
    for feed_url, entry_id, url in todo:
        netloc = urlparse(url).netloc.lower()
        if host_fail[netloc] >= _HOST_FAILURE_LIMIT:
            continue
        wait = _PACE_SECONDS - (time.monotonic() - last_request)
        if wait > 0:
            time.sleep(wait)
        hwait = _HOST_PACE_SECONDS - (time.monotonic() - host_last.get(netloc, 0.0))
        if hwait > 0:
            time.sleep(hwait)
        last_request = host_last[netloc] = time.monotonic()
        try:
            # The same call the live capture makes, so the storage, dedupe and
            # size cap cannot drift from it.
            # main.starred_archive_service is the configured INSTANCE; the
            # module import above only carries the constants.
            main.starred_archive_service._archive_asset(
                feed_url, entry_id, url,
                max_bytes=starred_archive_service.ATTACHMENT_MAX_BYTES,
            )
        except Exception as exc:  # noqa: BLE001
            host_fail[netloc] += 1
            print(f"    skip {url[:70]}: {type(exc).__name__}: {exc}", flush=True)
            continue
        with main.get_starred_archive_connection() as conn:
            ok = conn.execute(
                "SELECT 1 FROM archived_asset_link"
                " WHERE feed_url = ? AND entry_id = ? AND source_url = ?",
                (feed_url, entry_id, url),
            ).fetchone()
        if ok:
            stored.append({"feed_url": feed_url, "entry_id": entry_id, "source_url": url})
            print(f"    saved {url.split('/')[-1][:60]}", flush=True)

    print(f"  stored {len(stored):,} attachment(s)", flush=True)
    if stored:
        out = tenancy.meta_db_path().parent / f"backfilled_attachments_{datetime.now():%Y%m%d-%H%M%S}.json"
        out.write_text(json.dumps(stored, indent=2))
        print(f"  Log: {out}", flush=True)
    return len(stored)


def main_cli() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="fetch and store (default: dry run)")
    ap.add_argument("--user", default=None, help="restrict to one user_id")
    ap.add_argument("--feed", default=None, help="restrict to one feed URL")
    args = ap.parse_args()

    for uid in ([args.user] if args.user else main._background_user_ids()):
        with tenancy.user_context(uid):
            backfill_for_user(uid, args.apply, args.feed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
