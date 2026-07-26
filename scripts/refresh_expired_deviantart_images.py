"""Re-sign DeviantArt image URLs whose token has expired.

DeviantArt serves images from wixmp with a signed JWT in the query string.
Ordinary deviations get a permanently-signed URL, but **mature** ones are signed
with roughly a week's expiry — and every variant shares that expiry, so there is
no permanent thumbnail to fall back to (checked against the live API 2026-07-26:
content.src and both thumbs all carried exp=1785040938). Once it lapses the URL
answers 401 and the entry shows neither image nor thumbnail.

The only fix is to ask the API for a freshly-signed URL, which is what this does:
find stored entries whose image token is in the past, re-fetch those deviations,
and rewrite the stored HTML with the new URL.

This is periodic maintenance, not a one-off — a mature deviation re-expires about
a week after each refresh. Run it on a schedule if the library keeps any.

Usage (inside the app container):
    uv run scripts/refresh_expired_deviantart_images.py            # dry-run
    uv run scripts/refresh_expired_deviantart_images.py --apply
    uv run scripts/refresh_expired_deviantart_images.py --apply --within-days 2
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

import main  # noqa: E402
from services import tenancy  # noqa: E402

_TOKEN_RE = re.compile(r"token=([A-Za-z0-9_\-.]+)")
_API = "https://www.deviantart.com/api/v1/oauth2/deviation/"


def _token_exp(url: str) -> int | None:
    """Expiry epoch from a wixmp URL's JWT, or None when it never expires."""
    m = _TOKEN_RE.search(url or "")
    if not m:
        return None
    try:
        payload = m.group(1).split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("exp")
    except Exception:
        return None


def _setting(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return str(row[0]) if row and row[0] else ""


def find_expiring(within_seconds: float) -> list[dict]:
    """Stored DA entries whose image token is expired or about to be.

    Matching on the *stored HTML* rather than a maturity flag: we do not record
    is_mature, and the token is the thing that actually breaks.
    """
    cutoff = time.time() + within_seconds
    out: list[dict] = []
    with main.get_reader() as reader:
        rows = reader._storage.get_db().execute(
            "SELECT feed, id, title, summary, content FROM entries"
            " WHERE COALESCE(summary, '') LIKE '%wixmp%token=%'"
            "    OR COALESCE(content, '') LIKE '%wixmp%token=%'"
        ).fetchall()
    for feed, eid, title, summary, content in rows:
        for column, body in (("summary", summary), ("content", content)):
            if not body or "wixmp" not in body:
                continue
            exp = _token_exp(body)
            if exp is not None and exp < cutoff:
                out.append({"feed": str(feed), "entry_id": str(eid), "title": str(title or "")[:50],
                            "column": column, "exp": exp})
                break
    return out


def _fresh_url(deviation_id: str, token: str) -> str | None:
    try:
        resp = httpx.get(_API + deviation_id, timeout=20.0,
                         headers={"Authorization": f"Bearer {token}", "User-Agent": "Lectio/1.0"})
        if resp.status_code != 200:
            return None
        data = resp.json()
        return ((data.get("content") or {}).get("src")) or None
    except Exception:
        return None


def run_for_user(apply: bool, within_seconds: float, verbose: bool) -> dict:
    stale = find_expiring(within_seconds)
    if verbose:
        for s in stale[:15]:
            print(f"    exp={s['exp']}  {s['title']}")
        if len(stale) > 15:
            print(f"    … and {len(stale) - 15} more")
    if not apply or not stale:
        return {"stale": len(stale), "refreshed": 0}

    with main.get_meta_connection() as conn:
        token = _setting(conn, "deviantart_access_token")
    if not token:
        print("    no DeviantArt access token stored — connect DA first")
        return {"stale": len(stale), "refreshed": 0}

    refreshed = 0
    with main.get_reader() as reader:
        db = reader._storage.get_db()
        for s in stale:
            # The stored entry id is the deviation id for DA feeds.
            fresh = _fresh_url(s["entry_id"], token)
            if not fresh:
                continue
            row = db.execute(
                f"SELECT {s['column']} FROM entries WHERE feed = ? AND id = ?",  # nosemgrep
                (s["feed"], s["entry_id"]),
            ).fetchone()
            if not row or not row[0]:
                continue
            old_url_match = re.search(r'src="([^"]*wixmp[^"]*)"', row[0])
            if not old_url_match:
                continue
            updated = row[0].replace(old_url_match.group(1), fresh.replace("&", "&amp;"))
            db.execute(
                f"UPDATE entries SET {s['column']} = ? WHERE feed = ? AND id = ?",  # nosemgrep
                (updated, s["feed"], s["entry_id"]),
            )
            refreshed += 1
            time.sleep(0.3)  # be polite to the API
        db.commit()
    return {"stale": len(stale), "refreshed": refreshed}


def main_cli() -> None:
    ap = argparse.ArgumentParser(description="Re-sign expired DeviantArt image URLs.")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--user", default=None)
    ap.add_argument("--within-days", type=float, default=0.0,
                    help="also refresh tokens expiring within N days (default: only already-expired)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    users = [args.user] if args.user else main._background_user_ids()
    print(f"refresh expired DeviantArt images — {'APPLY' if args.apply else 'DRY-RUN'} — users: {users}\n")
    for uid in users:
        print(f"[{uid}]")
        with tenancy.user_context(uid):
            s = run_for_user(args.apply, args.within_days * 86400, not args.quiet)
        print(f"  {s}\n")
    if not args.apply:
        print("Dry-run only — re-run with --apply.")


if __name__ == "__main__":
    main_cli()
