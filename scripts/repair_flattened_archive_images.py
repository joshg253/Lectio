"""Re-capture archived images whose transparency was flattened to black.

The archive normalized images with ``convert("RGBA" if "A" in img.mode else
"RGB")``. For a **palette** PNG — mode ``"P"``, transparency living in
``img.info`` — that test is False, so exactly the images with transparency were
the ones converted to RGB. ``convert("RGB")`` keeps whatever sits *under* the
alpha, which for line art is black, so xkcd/what-if illustrations, logos and
diagrams were stored as solid black rectangles.

Fixed at the source in `services/starred_archive`, but **already-stored assets
are baked**: the alpha is gone from the stored bytes and only a re-fetch can
bring it back.

**Finding candidates is done from the WebP header, not by decoding.** A stored
asset that declares no alpha, whose source URL is a format that *can* carry
alpha, is a suspect — that is a 32-byte read per asset instead of decoding tens
of thousands of images (a full decode scan did not finish in ten minutes).

**A candidate is only repaired if the re-fetched source actually has alpha.** A
PNG that was always opaque is left alone: it was stored correctly and re-encoding
it would churn bytes for nothing.

Politeness: one request per asset, paced globally and per host, honest UA,
`url_guard` for SSRF safety. Hosts that fail repeatedly are dropped.

Asset rows are content-addressed by ``sha256(stored_bytes)``, so a repaired asset
gets a NEW hash: it is inserted, every link is repointed, and the orphaned old
row is deleted. The archived HTML stores original URLs (the ``/starred-asset/``
swap happens at render time), so nothing needs rewriting there.

    uv run python scripts/repair_flattened_archive_images.py --host what-if.xkcd.com
    uv run python scripts/repair_flattened_archive_images.py --host what-if.xkcd.com --apply
    uv run python scripts/repair_flattened_archive_images.py --apply      # everything
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import time
import warnings
from collections import defaultdict
from datetime import datetime
from urllib.parse import urlparse

warnings.filterwarnings("ignore")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image as _PILImage  # noqa: E402

import main  # noqa: E402
from services import starred_archive as starred_archive_service  # noqa: E402
from services import tenancy, url_guard  # noqa: E402

# Formats that can carry alpha; a JPEG source cannot, so it is never a suspect.
_ALPHA_CAPABLE_EXT = (".png", ".gif", ".webp")
_PACE_SECONDS = 0.5           # global floor between requests
_HOST_PACE_SECONDS = 1.5      # and a wider gap per host
_HOST_FAILURE_LIMIT = 3


def _declares_alpha(header: bytes) -> bool | None:
    """Whether a stored WebP says it has alpha, from its header alone.

    None when the bytes are not a WebP we recognize — those are left alone
    rather than guessed at.
    """
    if len(header) < 21 or header[:4] != b"RIFF" or header[8:12] != b"WEBP":
        return None
    fourcc = header[12:16]
    if fourcc == b"VP8X":
        return bool(header[20] & 0x10)     # ALPHA flag in the extended header
    if fourcc == b"VP8L":
        return True                        # lossless webp carries alpha
    if fourcc == b"VP8 ":
        return False                       # simple lossy: no alpha channel
    return None


def _reencode_with_alpha(raw: bytes) -> bytes | None:
    """Re-encode a source image to WebP keeping transparency, or None if it has
    none to keep (i.e. the stored copy was right all along)."""
    img = _PILImage.open(io.BytesIO(raw))
    if getattr(img, "is_animated", False):
        return None
    has_alpha = "A" in img.mode or "transparency" in img.info
    if not has_alpha:
        return None
    img = img.convert("RGBA")
    # An alpha channel that is entirely opaque carries no information, so
    # nothing was lost when it was dropped and the stored copy is faithful.
    # Without this the script "repairs" such images into byte-identical output
    # and reports a fix that never happened — which is exactly what it did for
    # xkcd's book covers, whose artwork is simply dark (mean luminance 38 on
    # white, straight from the publisher).
    if img.split()[3].getextrema() == (255, 255):
        return None
    longest = max(img.width, img.height)
    cap = starred_archive_service.ARCHIVE_IMAGE_MAX_DIM
    if longest > cap:
        scale = cap / longest
        img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                         _PILImage.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="WEBP",
             quality=starred_archive_service.ARCHIVE_IMAGE_WEBP_QUALITY, method=4)
    return buf.getvalue()


def _candidates(conn, host: str | None) -> list[tuple[str, str]]:
    """(asset_hash, source_url) for assets that declare no alpha but came from a
    format that can carry it. Reads 32 bytes per asset, not the whole blob."""
    sql = """
        SELECT a.asset_hash, MIN(l.source_url) AS src, substr(a.data, 1, 32) AS head
          FROM archived_asset a
          JOIN archived_asset_link l ON l.asset_hash = a.asset_hash
         GROUP BY a.asset_hash
    """
    out: list[tuple[str, str]] = []
    seen = 0
    for asset_hash, src, head in conn.execute(sql):
        seen += 1
        if seen % 2000 == 0:
            print(f"    scanned {seen:,} asset(s), {len(out):,} candidate(s) so far", flush=True)
        src = str(src or "")
        path = urlparse(src).path.lower()
        if not path.endswith(_ALPHA_CAPABLE_EXT):
            continue
        if host and urlparse(src).netloc.lower() != host.lower():
            continue
        if _declares_alpha(bytes(head)) is not False:
            continue          # already has alpha, or not a WebP we understand
        out.append((asset_hash, src))
    return out


def repair_for_user(user_id: str, apply: bool, host: str | None, limit: int) -> int:
    print(f"[{user_id}] scanning the archive for flattened images…", flush=True)
    with main.get_starred_archive_connection() as conn:
        cands = _candidates(conn, host)
    print(f"[{user_id}] {len(cands):,} candidate asset(s)"
          + (f" on {host}" if host else "") + " — re-fetching to check for alpha",
          flush=True)
    if not cands:
        return 0

    repaired: list[dict] = []
    host_fail: dict[str, int] = defaultdict(int)
    host_last: dict[str, float] = {}
    last_request = 0.0

    for asset_hash, src in cands[:limit] if limit else cands:
        netloc = urlparse(src).netloc.lower()
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
            with url_guard.build_client(
                timeout=20.0, follow_redirects=True,
                headers={"User-Agent": main.READABILITY_USER_AGENT},
            ) as client:
                resp = url_guard.safe_get(client, src)
            resp.raise_for_status()
            fresh = _reencode_with_alpha(resp.content)
        except Exception as exc:  # noqa: BLE001
            host_fail[netloc] += 1
            print(f"    skip {src[:70]}: {type(exc).__name__}", flush=True)
            continue
        if fresh is None:
            continue          # source has no transparency; stored copy was fine

        new_hash = hashlib.sha256(fresh).hexdigest()
        repaired.append({"old_hash": asset_hash, "new_hash": new_hash,
                         "source_url": src, "bytes": len(fresh)})
        print(f"    fixed {src.split('/')[-1][:44]}  {len(fresh):,}B", flush=True)

        if not apply:
            continue
        with main.get_starred_archive_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO archived_asset"
                " (asset_hash, data, content_type, width, height, byte_size, created_at)"
                " VALUES (?, ?, 'image/webp',"
                "  (SELECT width FROM archived_asset WHERE asset_hash = ?),"
                "  (SELECT height FROM archived_asset WHERE asset_hash = ?), ?, ?)",
                (new_hash, fresh, asset_hash, asset_hash, len(fresh), time.time()),
            )
            conn.execute("UPDATE archived_asset_link SET asset_hash = ? WHERE asset_hash = ?",
                         (new_hash, asset_hash))
            conn.execute("DELETE FROM archived_asset WHERE asset_hash = ?"
                         " AND NOT EXISTS (SELECT 1 FROM archived_asset_link WHERE asset_hash = ?)",
                         (asset_hash, asset_hash))
            conn.commit()

    print(f"  {len(repaired):,} asset(s) had transparency to restore")
    if repaired and apply:
        out = tenancy.meta_db_path().parent / f"repaired_flat_images_{datetime.now():%Y%m%d-%H%M%S}.json"
        out.write_text(json.dumps(repaired, indent=2))
        print(f"  written. Log: {out}")
    elif repaired:
        print("  dry run — re-run with --apply to write")
    return len(repaired)


def main_cli() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--user", default=None, help="restrict to one user_id")
    ap.add_argument("--host", default=None, help="restrict to one image host")
    ap.add_argument("--limit", type=int, default=0, help="stop after N candidates")
    args = ap.parse_args()

    for uid in ([args.user] if args.user else main._background_user_ids()):
        with tenancy.user_context(uid):
            repair_for_user(uid, args.apply, args.host, args.limit)
    if args.apply:
        print("\nRestart the app so nothing serves a cached copy of the old asset.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
