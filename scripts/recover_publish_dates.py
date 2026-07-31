"""Recover real publish dates for epoch-dated entries, offline.

The Instapaper importer stored your *save* timestamp as the article's publish date
(fixed at the source; see `repair_fabricated_publish_dates.py`), so those entries
now sit at the Unix epoch — visibly unknown and sorted to the end. 3,308 of them.

**Nothing is fetched.** Every date comes from HTML Lectio already captured into the
starred archive, so this costs no requests and works on articles whose sites are
long dead. Measured when written: 2,984 of the 3,308 have stored page HTML, and
2,219 yield a date.

Sources, in order of trust:

1. ``article:published_time`` — the Open Graph field, by far the most common
   (2,088 of the hits) and the one publishers actually maintain;
2. JSON-LD ``datePublished`` (87);
3. ``itemprop="datePublished"`` (2);
4. ``<time datetime=…>`` (42) — **last**, because a page has many `<time>` tags and
   the first one may belong to a comment or a "latest posts" rail;
5. the entry URL's own ``/2019/07/06/`` path, for entries with no stored HTML.

**The save date is an upper bound, and it is what makes tier 4 safe.** The HTML was
captured *recently*, not when the article was saved — so a stray `<time>` can be
years newer than the article. You cannot save something before it is published, so
any candidate later than the Instapaper save timestamp (plus a day of slack for
timezone sloppiness) is rejected rather than trusted.

Manual ``entry_date_overrides`` are never touched: an explicit correction outranks
anything inferred here.

    uv run python scripts/recover_publish_dates.py            # dry run
    uv run python scripts/recover_publish_dates.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import zlib
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from services import tenancy  # noqa: E402

EPOCH_PREFIX = "1970-01-01"
_MIN_YEAR, _MAX_YEAR = 1990, 2027
# A day of slack: save timestamps and publish times disagree about timezone often
# enough that an exact comparison would reject same-day saves.
_SAVE_SLACK = timedelta(days=1)

_SOURCES: list[tuple[str, re.Pattern[str]]] = [
    ("og:article:published_time", re.compile(
        r'<meta[^>]+(?:property|name)=["\']article:published_time["\'][^>]*content=["\']([^"\']+)', re.I)),
    ("og:reversed-attr-order", re.compile(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']article:published_time["\']', re.I)),
    ("json-ld:datePublished", re.compile(r'"datePublished"\s*:\s*"([^"]+)"', re.I)),
    ("itemprop:datePublished", re.compile(
        r'<meta[^>]+itemprop=["\']datePublished["\'][^>]*content=["\']([^"\']+)', re.I)),
    ("meta:date", re.compile(
        r'<meta[^>]+name=["\'](?:date|pubdate|publish-date|DC\.date[^"\']*)["\'][^>]*content=["\']([^"\']+)', re.I)),
    # Last: a page has many <time> tags and the first may be a comment's.
    ("time:datetime", re.compile(r'<time[^>]+datetime=["\']([^"\']+)', re.I)),
]


def _parse(value: str) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw)
        if not m:
            return None
        try:
            dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if not (_MIN_YEAR < dt.year < _MAX_YEAR):
        return None
    return dt


def recover_for_user(uid: str, apply: bool) -> int:
    meta = sqlite3.connect(f"file:{tenancy.meta_db_path()}?mode=ro", uri=True, timeout=30.0)
    meta.row_factory = sqlite3.Row
    saved_at = {}
    for row in meta.execute("SELECT feed_url, entry_id, saved_at FROM saved_entries"):
        dt = main._parse_stored_dt(row["saved_at"])
        if dt is not None:
            saved_at[(str(row["feed_url"]), str(row["entry_id"]))] = (
                dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))
    overrides = {
        (str(r["feed_url"]), str(r["entry_id"]))
        for r in meta.execute("SELECT feed_url, entry_id FROM entry_date_overrides")
    }
    meta.close()

    rc = sqlite3.connect(str(tenancy.reader_db_path()), timeout=30.0)
    rc.row_factory = sqlite3.Row
    rows = rc.execute(
        "SELECT feed, id, link, title FROM entries WHERE published LIKE ?", (EPOCH_PREFIX + "%",)
    ).fetchall()

    ac = sqlite3.connect(f"file:{tenancy.starred_archive_db_path()}?mode=ro", uri=True, timeout=30.0)

    found: list[dict] = []
    stats = {"no_html": 0, "no_date": 0, "rejected_after_save": 0, "override_kept": 0}
    by_source: dict[str, int] = {}

    for row in rows:
        key = (str(row["feed"]), str(row["id"]))
        if key in overrides:
            stats["override_kept"] += 1
            continue
        limit = saved_at.get(key)
        candidate = source = None

        arow = ac.execute(
            "SELECT source_html_zlib FROM archived_entry WHERE feed_url = ? AND entry_id = ?", key
        ).fetchone()
        html = None
        if arow and arow[0]:
            try:
                html = zlib.decompress(arow[0]).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 — a corrupt blob is just a miss
                html = None
        if html:
            for name, pat in _SOURCES:
                for m in pat.finditer(html):
                    dt = _parse(m.group(1))
                    if dt is None:
                        continue
                    # The upper bound is what makes the <time> tier safe: the HTML
                    # was captured recently, so a stray tag can postdate the save.
                    if limit is not None and dt > limit + _SAVE_SLACK:
                        continue
                    candidate, source = dt, name
                    break
                if candidate is not None:
                    break
        else:
            stats["no_html"] += 1

        if candidate is None:
            url_dt = (main.url_inferred_pubdate(row["link"])
                      or main.url_inferred_pubdate(str(row["id"])))
            if url_dt is not None:
                if url_dt.tzinfo is None:
                    url_dt = url_dt.replace(tzinfo=timezone.utc)
                if limit is None or url_dt <= limit + _SAVE_SLACK:
                    candidate, source = url_dt, "url-path"

        if candidate is None:
            if html:
                stats["no_date"] += 1
            continue

        found.append({
            "feed_url": key[0], "entry_id": key[1],
            "title": str(row["title"] or "")[:70],
            "published": candidate.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "source": source,
        })
        by_source[source] = by_source.get(source, 0) + 1

    ac.close()
    print(f"[{uid}] {len(rows):,} epoch-dated; {len(found):,} recoverable")
    for k, v in sorted(by_source.items(), key=lambda kv: -kv[1]):
        print(f"     {v:>6,}  {k}")
    print(f"     {stats['no_html']:>6,}  (no stored page HTML)")
    print(f"     {stats['no_date']:>6,}  (HTML but no usable date)")
    if stats["override_kept"]:
        print(f"     {stats['override_kept']:>6,}  (manual override — untouched)")
    for f in found[:8]:
        print(f"   {f['published'][:10]}  {f['source']:<26} {f['title'][:44]}")

    if not apply or not found:
        if not apply:
            print("  dry run — re-run with --apply to write")
        rc.close()
        return len(found)

    for start in range(0, len(found), 500):
        rc.executemany(
            "UPDATE entries SET published = ? WHERE feed = ? AND id = ?",
            [(f["published"], f["feed_url"], f["entry_id"]) for f in found[start:start + 500]],
        )
    rc.commit()
    rc.close()

    out = tenancy.meta_db_path().parent / f"recovered_publish_dates_{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(found, indent=2))
    print(f"  set {len(found):,} publish date(s). Log: {out}")
    return len(found)


def main_cli() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--user", default=None, help="restrict to one user_id")
    args = ap.parse_args()

    for uid in ([args.user] if args.user else main._background_user_ids()):
        with tenancy.user_context(uid):
            recover_for_user(uid, args.apply)
    if args.apply:
        print("\nRestart the app: the unread-count cache is generation-guarded and "
              "will not self-heal from a behind-the-back write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
