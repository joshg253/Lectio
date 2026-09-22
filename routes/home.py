"""Home/reader surface (Plan.md's main.py/index.html breakup, Stage 10 of the
route-by-URL-prefix split -- the last route module in the whole project,
`/`, `/read`, `/read/offline`, and the shared rendering core itself).

**Stage 10 is NOT done: this file currently holds only sub-stage A's 1 route,
`GET /read/offline`.** Sub-stages B (`/read`) and C (`/`) add more routes to
this same module in later tasks -- don't assume this is the final state. The
shared rendering-core functions (`_home_inner`, `list_entries_for_feeds`,
`get_entry_detail`, `build_reader_page`, `resolve_reader_article_html`) all
stay in main.py regardless of how many of Stage 10's routes eventually move
here -- see Plan.md's Landmines note.

Stage 10A -- `GET /read/offline` alone. Landmines-flagged going in as the
cluster to treat carefully, but this individual route turned out to be the
safe pick Stage 9E already proved the pattern for: `read_offline_copy` is pure
orchestration -- one `get_entry_detail` call, one `resolve_reader_article_html`
call, then string/HTML assembly with no session or pane-state coupling. It
does NOT call `_home_inner`, `list_entries_for_feeds`, or `build_reader_page`;
of the shared rendering-core functions it only touches `get_entry_detail` and
`resolve_reader_article_html`, and both stay in main.py untouched, imported
back like every other main.py-resident helper.

The route's own `Path(__file__).parent` for locating `static/reader.css` hit
the same relocation bug Stage 1 found in `offline_service_worker` (`/sw.js`)
-- once moved into `routes/`, `__file__` resolves one directory too deep --
fixed the same way, with the existing `BASE_DIR` constant instead.

Three helpers moved with the route, confirmed via the `routes/*.py` +
`scripts/*.py` + `tests/` three-way grep to have no caller anywhere else:
`_fetch_image_for_offline`, `_inline_images_as_data_uris`, and the
`_OFFLINE_IMG_MAX_BYTES`/`_OFFLINE_IMG_TOTAL_BYTES`/`_OFFLINE_IMG_MAX_FETCHES`
constants -- all exist only to serve this one download route. `_read_mode_date`
stayed in main.py and is imported back: it has a second still-in-main.py
caller, `reader_view` (`/read`, Stage 10B). `api_img_proxy`, `_img_cache_get`,
and `_img_cache_key_url` also stayed -- all three are widely-shared
image-cache primitives with several other callers across main.py and
`routes/system.py` already.

No `services.automation_rules` ordering constraint: this route touches
nothing from that late import, so it's imported alongside the plain
`routes.compat_*`/`routes.tags`-style modules. No test exercises `/read/
offline` or any of its moved helpers today -- the only "read_offline" hit in
`tests/` is an unrelated string-literal assertion in
`tests/unit/test_offline_outbox.py` about a different, non-existent
`/read/offline/manifest` path -- so neither of the usual gotchas needed
fixing and no test file was retargeted. No `scripts/*.py` callers turned up
either.
"""

from __future__ import annotations

import base64
import hashlib
import html
import re
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, JSONResponse

from main import (
    BASE_DIR,
    LOGGER,
    _img_cache_get,
    _img_cache_key_url,
    _read_mode_date,
    api_img_proxy,
    get_entry_detail,
    html_sanitize,
    resolve_reader_article_html,
)

router = APIRouter()


_OFFLINE_IMG_MAX_BYTES = 2 * 1024 * 1024  # per image, inlined as base64
_OFFLINE_IMG_TOTAL_BYTES = 12 * 1024 * 1024  # whole document budget
_OFFLINE_IMG_MAX_FETCHES = 20  # network fetches per saved article


def _fetch_image_for_offline(url: str) -> tuple[bytes, str] | None:
    """Fetch one image through the proxy's own route, so the SSRF guard, the
    honest UA and the cache write are exactly those of a normal image load —
    rather than a second, subtly different outbound path."""
    import asyncio

    resp = asyncio.run(api_img_proxy(url))
    body = getattr(resp, "body", b"") or b""
    ctype = resp.headers.get("content-type", "")
    if resp.status_code != 200 or not ctype.startswith("image/") or not body:
        return None
    return bytes(body), ctype


def _inline_images_as_data_uris(article_html: str) -> tuple[str, int, int]:
    """Rewrite <img src> to data: URIs so the document needs no network at all.

    Serves the single-file download route below, which remains a SPARE — the
    service worker is what offline reading actually runs on. It is kept because
    it fails differently: the Supernote has no browser
    launcher — Read Mode is reached from a saved hyperlink — so with WiFi off the
    navigation itself fails and nothing cached is reachable. A downloaded,
    self-contained file sidesteps the entry-point problem entirely, which is the
    thing a service worker cannot be assumed to solve in a WebView.

    Images come from the /api/img byte cache when it has them (zero extra
    requests). Misses are fetched — bounded, and only because this is an explicit
    "save this for offline" click rather than anything speculative; the cache
    turns out to hold only recently-viewed images (1,800 rows), so cache-only
    would leave most saved articles full of broken pictures. The fetch reuses the
    proxy's own path, so the SSRF guard, the polite UA and the cache write all
    behave identically to a normal image load.

    → (html, inlined_count, skipped_count)
    """
    if not article_html or "<img" not in article_html.lower():
        return article_html, 0, 0

    from bs4 import BeautifulSoup  # imported at use site, as elsewhere in main

    soup = BeautifulSoup(article_html, "html.parser")
    inlined = skipped = fetched = 0
    budget = _OFFLINE_IMG_TOTAL_BYTES
    for img in soup.find_all("img"):
        src = str(img.get("src") or "")
        if not src or src.startswith("data:"):
            continue
        # Unwrap our own proxy URL back to the real one before hashing: the cache
        # is keyed on the upstream URL, not on /api/img?u=…
        target = src
        if "/api/img" in src:
            qs = parse_qs(urlparse(src).query)
            target = (qs.get("u") or [""])[0] or src
        try:
            key = hashlib.sha256(_img_cache_key_url(target).encode("utf-8")).hexdigest()
            hit = _img_cache_get(key)
        except Exception:  # noqa: BLE001 — an offline copy is best-effort
            hit = None
        if hit is None and fetched < _OFFLINE_IMG_MAX_FETCHES:
            fetched += 1
            try:
                hit = _fetch_image_for_offline(target)
            except Exception:  # noqa: BLE001 — best-effort
                hit = None
        if hit is None or len(hit[0]) > _OFFLINE_IMG_MAX_BYTES or len(hit[0]) > budget:
            skipped += 1
            continue
        body, content_type = hit
        budget -= len(body)
        img["src"] = f"data:{content_type or 'image/jpeg'};base64," + base64.b64encode(body).decode("ascii")
        # srcset would re-introduce network fetches the moment the browser
        # preferred one of its candidates over the inlined src.
        for attr in ("srcset", "data-src", "data-srcset", "loading"):
            if img.has_attr(attr):
                del img[attr]
        inlined += 1
    return str(soup), inlined, skipped


@router.get("/read/offline")
def read_offline_copy(
    feed_url: str = Query(...),
    entry_id: str = Query(...),
):
    """Download one article as a single self-contained HTML file.

    A SPARE, not the main route. Offline reading runs on the service worker,
    which the Supernote's browser turned out to support; this was the cheap probe
    that ran first, and it is kept because it fails differently — no JS at all,
    so nothing here can break on an old WebView.

    No JS, no external references, CSS inlined, images as data: URIs. Nothing in
    it can phone home, so if it opens offline it works offline.
    """
    detail = get_entry_detail(feed_url, entry_id)
    if detail is None:
        return JSONResponse({"ok": False, "error": "No such entry."}, status_code=404)
    title = str(detail.get("title") or detail.get("link") or "(untitled)")
    link = str(detail.get("link") or "")
    article_html = resolve_reader_article_html(feed_url, entry_id, link)
    article_html, inlined, skipped = _inline_images_as_data_uris(article_html)

    try:
        css = (BASE_DIR / "static" / "reader.css").read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        css = ""
        LOGGER.warning("offline copy: reader.css unreadable", exc_info=True)

    date_display = _read_mode_date(detail)
    # <title> cannot contain markup, so it gets the tag-stripped text; the <h1>
    # gets the same inline allowlist the on-screen reader uses.
    esc_title = html.escape(html_sanitize.title_plain_text(title))
    head_title = html_sanitize.sanitize_inline_title(title)
    doc = (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
        f"<title>{esc_title}</title>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<style>{css}</style>"
        # The saved file is read as one long scroll, not paged: pagination is
        # reader.js's job and there is no JS here by design.
        "<style>body{overflow:auto}.reader-columns{columns:auto!important;"
        "height:auto!important;overflow:visible!important}</style>"
        "</head><body class='reader-body'>"
        "<main class='reader-columns'>"
        f"<h1 class='reader-title'>{head_title}</h1>"
        + (f"<p class='reader-dateline'>{html.escape(date_display)}</p>" if date_display else "")
        + article_html
        + (f"<p class='reader-dateline'>Source: {html.escape(link)}</p>" if link else "")
        + f"<p class='reader-dateline'>Offline copy — {inlined} image(s) embedded, {skipped} skipped.</p>"
        "</main></body></html>"
    )
    slug = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-").lower()[:60] or "article"
    return HTMLResponse(
        doc,
        headers={
            "Content-Disposition": f'attachment; filename="{slug}.html"',
            "Cache-Control": "no-store",
        },
    )
