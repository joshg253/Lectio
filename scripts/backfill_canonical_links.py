"""Backfill canonical links for starred entries stuck on dead feed-redirector
URLs (feedproxy.google.com, feeds.feedburner.com, FeedsPortal).

Recovery order per starred entry whose reader `entries.link` is a redirector:
  1. The starred archive's captured source page HTML (fetched while the
     redirector still worked): extract <link rel="canonical"> or og:url.
  2. Live resolution: follow the redirect chain (works only while the
     redirector service still answers — feedproxy.google.com won't).

Applies: reader `entries.link`, a meta `entry_link_overrides` row (so feed
refreshes can't revert it), and the archive row's own `link` column.

Usage:
    LECTIO_DATA_DIR=/data uv run python scripts/backfill_canonical_links.py [--user <uid>] [--apply] [--live-resolve]

Dry-run by default: prints old -> new without writing. --live-resolve enables
step 2's network fetches (off by default: archive recovery is free).
"""
from __future__ import annotations

import argparse
import html as html_mod
import os
import re
import sqlite3
import sys
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.link_canonical import is_redirector_link  # noqa: E402

_CANONICAL_RE = re.compile(
    r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']|'
    r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\']canonical["\']',
    re.IGNORECASE,
)
_OG_URL_RE = re.compile(
    r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)["\']|'
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:url["\']',
    re.IGNORECASE,
)


def canonical_from_html(page_html: str) -> str | None:
    for pattern in (_CANONICAL_RE, _OG_URL_RE):
        m = pattern.search(page_html)
        if m:
            url = html_mod.unescape((m.group(1) or m.group(2) or "").strip())
            if url.startswith(("http://", "https://")) and not is_redirector_link(url):
                return url
    return None


_WAYBACK_SNAPSHOT_RE = re.compile(r"/web/\d+[a-z_]*/(https?://.+)$")


def strip_tracking_params(url: str) -> str:
    """Drop utm_* params (FeedBurner appended them to every redirect target)."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    parts = urlsplit(url)
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not k.lower().startswith("utm_")]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))


def resolve_wayback(url: str) -> str | None:
    """Recover a dead redirector's target from the Wayback Machine: the
    snapshot of the redirector URL 301s to the archived REAL URL, which is
    embedded in the wayback path (/web/<ts>/<original>)."""
    import httpx
    try:
        with httpx.Client(follow_redirects=True, timeout=25.0,
                          headers={"User-Agent": "Lectio/1.0 (+https://github.com/joshg253/Lectio)"}) as client:
            resp = client.get(f"https://web.archive.org/web/2/{url}")
        m = _WAYBACK_SNAPSHOT_RE.search(str(resp.url))
        if not m:
            return None
        orig = strip_tracking_params(m.group(1))
        if is_redirector_link(orig) or "web.archive.org" in orig:
            return None
        return orig
    except Exception:
        return None


def resolve_live(url: str) -> str | None:
    from services import url_guard  # deferred: needs httpx
    try:
        with url_guard.build_client(timeout=10.0, headers={"User-Agent": "Lectio/1.0 (+https://github.com/joshg253/Lectio)"}) as client:
            resp = url_guard.safe_get(client, url, headers={"User-Agent": "Lectio/1.0"})
        final = str(resp.url)
        if final != url and not is_redirector_link(final):
            return final
    except Exception:
        return None
    return None


def user_dirs(data_dir: Path) -> list[tuple[str, Path]]:
    dirs = [("<legacy>", data_dir)]
    users_root = data_dir / "users"
    if users_root.is_dir():
        for d in sorted(users_root.iterdir()):
            if d.is_dir():
                dirs.append((d.name, d))
    return dirs


def process_user(label: str, d: Path, *, apply: bool, live: bool, wayback: bool) -> tuple[int, int]:
    meta_path = d / "lectio_meta.sqlite3"
    reader_path = d / "lectio_reader.sqlite"
    archive_path = d / "lectio_starred_archive.sqlite"
    if not meta_path.exists() or not reader_path.exists():
        return 0, 0

    meta = sqlite3.connect(str(meta_path))
    meta.row_factory = sqlite3.Row
    reader = sqlite3.connect(str(reader_path))
    reader.row_factory = sqlite3.Row
    archive = None
    if archive_path.exists():
        archive = sqlite3.connect(str(archive_path))
        archive.row_factory = sqlite3.Row

    found = fixed = 0
    try:
        saved = meta.execute("SELECT feed_url, entry_id FROM saved_entries").fetchall()
        for row in saved:
            feed_url, entry_id = str(row["feed_url"]), str(row["entry_id"])
            ent = reader.execute(
                "SELECT link FROM entries WHERE feed = ? AND id = ?", (feed_url, entry_id)
            ).fetchone()
            link = str(ent["link"] or "") if ent else ""
            if not link or not is_redirector_link(link):
                continue
            found += 1

            new_link = None
            if archive is not None:
                arc = archive.execute(
                    "SELECT source_html_zlib FROM archived_entry WHERE feed_url = ? AND entry_id = ?",
                    (feed_url, entry_id),
                ).fetchone()
                if arc and arc["source_html_zlib"]:
                    try:
                        page = zlib.decompress(arc["source_html_zlib"]).decode("utf-8", errors="replace")
                        new_link = canonical_from_html(page)
                    except Exception:
                        pass
            if new_link is None and live:
                new_link = resolve_live(link)
            if new_link is None and wayback:
                new_link = resolve_wayback(link)
                time.sleep(1.5)  # polite pacing for archive.org
            if new_link is None:
                print(f"  [unrecoverable] {link}")
                continue

            print(f"  {link}\n    -> {new_link}")
            if apply:
                reader.execute(
                    "UPDATE entries SET link = ? WHERE feed = ? AND id = ?",
                    (new_link, feed_url, entry_id),
                )
                meta.execute(
                    "INSERT OR REPLACE INTO entry_link_overrides (feed_url, entry_id, link) VALUES (?, ?, ?)",
                    (feed_url, entry_id, new_link),
                )
                if archive is not None:
                    archive.execute(
                        "UPDATE archived_entry SET link = ? WHERE feed_url = ? AND entry_id = ?",
                        (new_link, feed_url, entry_id),
                    )
            fixed += 1
        if apply:
            reader.commit()
            meta.commit()
            if archive is not None:
                archive.commit()
    finally:
        meta.close()
        reader.close()
        if archive is not None:
            archive.close()
    return found, fixed


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--user", help="only this user id (default: legacy + all users)")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--live-resolve", action="store_true", help="follow still-alive redirectors over the network")
    ap.add_argument("--wayback", action="store_true", help="recover dead redirectors via the Wayback Machine's archived redirects (rate-limited, ~2s/link)")
    args = ap.parse_args()

    data_dir = Path(os.environ.get("LECTIO_DATA_DIR", "data")).resolve()
    if not data_dir.is_dir():
        sys.exit(f"data dir not found: {data_dir} (set LECTIO_DATA_DIR)")

    total_found = total_fixed = 0
    for label, d in user_dirs(data_dir):
        if args.user and label != args.user:
            continue
        print(f"== {label} ({d})")
        found, fixed = process_user(label, d, apply=args.apply, live=args.live_resolve, wayback=args.wayback)
        print(f"   redirector links: {found}, recoverable: {fixed}")
        total_found += found
        total_fixed += fixed
    mode = "APPLIED" if args.apply else "DRY RUN"
    print(f"{mode}: {total_fixed}/{total_found} starred redirector links recoverable")


if __name__ == "__main__":
    main()
