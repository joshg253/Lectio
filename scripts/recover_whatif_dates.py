"""Recover publish dates for what-if.xkcd.com entries from the site's own archive.

**One request, real dates.** `fetch_missing_publish_dates.py` measured what-if as
one of the hopeless clusters — 45 entries, and fetching the article pages returned
**zero** dates. That measurement was right, and it was about the wrong page: a
what-if *article* carries no date metadata of any kind (verified again 2026-08-04
against all 50 stored captures — no `article:published_time`, no JSON-LD, not even
a `<time>` tag). The *archive index* carries all of them:

    <h2 class="archive-title"><a href="https://what-if.xkcd.com/1">Relativistic
    Baseball</a></h2>
    <h3 class="archive-date">July 10, 2012</h3>

So the whole back catalogue is one polite GET, keyed by the article URL Lectio
already stores as the entry id. Publisher-supplied, not inferred — no
interpolation between known articles, no Wayback first-capture approximation.

**The lesson worth keeping** (the same one `fetch_missing_publish_dates.py`
records for blog.guitar-pro.com): check whether a date is *published somewhere
else on the site* before concluding it must be fetched per-article, or that it
does not exist.

Only entries currently sitting on a missing-date sentinel are touched, manual
`entry_date_overrides` are never overwritten, and an entry whose number cannot be
read out of its URL is skipped rather than guessed at.

    uv run python scripts/recover_whatif_dates.py            # dry run
    uv run python scripts/recover_whatif_dates.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from services import (
    tenancy,  # noqa: E402
    url_guard,  # noqa: E402
)

ARCHIVE_URL = "https://what-if.xkcd.com/archive/"
_HOST = "what-if.xkcd.com"

# One archive entry: the article link, then its date in the following <h3>.
_ARCHIVE_ROW_RE = re.compile(
    r'<h2[^>]*class="archive-title"[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>.*?</h2>\s*'
    r'<h3[^>]*class="archive-date"[^>]*>\s*([^<]+?)\s*</h3>',
    re.I | re.S,
)
# The article number is the whole identity here: http/https, www or not, trailing
# slash or not, all name the same piece.
_NUM_RE = re.compile(r"//(?:www\.)?what-if\.xkcd\.com/(\d+)", re.I)


def _article_number(url: str) -> str | None:
    m = _NUM_RE.search(url or "")
    return m.group(1) if m else None


def fetch_archive_dates() -> dict[str, datetime]:
    """``{article_number: published}`` from the archive index. One request."""
    with url_guard.build_client(
        timeout=20.0, follow_redirects=True,
        headers={"User-Agent": main.READABILITY_USER_AGENT},
    ) as client:
        resp = url_guard.safe_get(client, ARCHIVE_URL)
    resp.raise_for_status()

    dates: dict[str, datetime] = {}
    for href, date_text in _ARCHIVE_ROW_RE.findall(resp.text):
        number = _article_number(href)
        if not number:
            continue
        try:
            dt = datetime.strptime(date_text.strip(), "%B %d, %Y").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        dates[number] = dt
    return dates


def recover_for_user(user_id: str, apply: bool) -> int:
    archive = fetch_archive_dates()
    print(f"[{user_id}] archive index: {len(archive):,} dated article(s)")
    if not archive:
        print("  archive gave nothing — the page's markup may have changed; not touching anything")
        return 0

    meta = sqlite3.connect(str(tenancy.meta_db_path()))
    meta.row_factory = sqlite3.Row
    protected = {
        (r["feed_url"], r["entry_id"])
        for r in meta.execute("SELECT feed_url, entry_id FROM entry_date_overrides")
    }
    meta.close()

    rc = sqlite3.connect(str(tenancy.reader_db_path()))
    rc.row_factory = sqlite3.Row
    rows = rc.execute(
        "SELECT feed, id, title, published FROM entries"
        " WHERE (feed LIKE ? OR id LIKE ?) AND published IS NOT NULL AND published < ?",
        (f"%{_HOST}%", f"%{_HOST}%", "1990-01-01"),
    ).fetchall()

    found: list[dict] = []
    unmatched: list[str] = []
    for row in rows:
        key = (str(row["feed"]), str(row["id"]))
        if key in protected:
            continue  # an explicit correction outranks anything recovered here
        number = _article_number(str(row["id"]))
        dt = archive.get(number) if number else None
        if dt is None:
            unmatched.append(str(row["id"]))
            continue
        found.append({
            "feed_url": key[0],
            "entry_id": key[1],
            "title": str(row["title"] or ""),
            "published": dt.strftime("%Y-%m-%d %H:%M:%S"),
            "source": "whatif-archive-index",
        })

    print(f"  {len(rows):,} sentinel-dated entr(ies); {len(found):,} matched, {len(unmatched):,} not")
    for f in found[:5]:
        print(f"    {f['published'][:10]}  {f['title'][:52]}")
    if unmatched[:3]:
        print(f"    unmatched e.g.: {unmatched[:3]}")

    if not apply or not found:
        if found:
            print("  dry run — re-run with --apply to write")
        rc.close()
        return len(found)

    rc.executemany(
        "UPDATE entries SET published = ? WHERE feed = ? AND id = ?",
        [(f["published"], f["feed_url"], f["entry_id"]) for f in found],
    )
    rc.commit()
    rc.close()

    out = tenancy.meta_db_path().parent / f"recovered_whatif_dates_{datetime.now():%Y%m%d-%H%M%S}.json"
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
