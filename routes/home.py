"""Home/reader surface (Plan.md's main.py/index.html breakup, Stage 10 of the
route-by-URL-prefix split -- the last route module in the whole project,
`/`, `/read`, `/read/offline`, and the shared rendering core itself).

**Stage 10 is NOT done yet: this file currently holds sub-stages A and B's 2
routes, `GET /read/offline` and `GET /read`.** Sub-stage C (`/`, the last and
highest-traffic route of the whole split) still adds one more route to this
same module in a later task -- don't assume this is the final state. The
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

Stage 10B -- `GET /read` alone (`reader_view`): Read Mode's whole 2-pane browse + full-screen paginated
reader, both states in one handler. The bigger, more central, Landmines-flagged-riskier of Stage 10's two
remaining routes -- checked closely rather than assumed clean, and it held up the same way 9E/10A did: query
normalization, one `resolve_reader_backlog` call for the node's item list, then either
`_build_feeds_mode_context`/`_build_read_mode_context` + `templates.TemplateResponse` (browse state) or a
walk of the already-fetched backlog for prev/current/next followed by `resolve_reader_article_html` +
`build_reader_page` (read state). No inline list-building or pagination logic of its own. Per this
sub-stage's explicit scope, ONLY the handler moved -- every function/constant it calls, including
`resolve_reader_backlog`, `_build_feeds_mode_context`, `_build_read_mode_context`, `get_entry_detail`,
`resolve_reader_article_html`, and `build_reader_page`, stayed in main.py untouched and got imported back,
same as `_read_mode_date` did for 10A. `_READ_MODE_UA_SEEN` (a mutable module-level set with no other
caller) stayed too rather than moving as a sole-caller helper the way 10A's offline helpers did -- this
sub-stage's scope was "move only the handler," and importing a mutable set back still works correctly since
it's mutated in place (`.add()`), never reassigned. No `services.automation_rules` ordering constraint.
`reader_view` is a real, actively-tested UI surface (unlike 10A's route): `tests/integration/test_reader_view.py`
hit both usual gotchas -- it registered `main.reader_view` directly on a bare test `FastAPI()` app (retargeted
to `routes.home.reader_view`) and monkeypatched several of the above names on `main` (retargeted to
`routes.home.<name>` wherever `reader_view` actually calls them). See that test file and this sub-stage's own
paragraph in `routes/__init__.py` for the full per-name breakdown. No `scripts/*.py` callers turned up.
"""

from __future__ import annotations

import base64
import hashlib
import html
import re
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from main import (
    _READ_MODE_UA_SEEN,
    _READ_SORT_DEFAULT,
    _READ_SORTS,
    BASE_DIR,
    LOGGER,
    _build_feeds_mode_context,
    _build_read_mode_context,
    _csrf_token_for,
    _img_cache_get,
    _img_cache_key_url,
    _read_browse_href,
    _read_is_inbox_node,
    _read_mode_date,
    _read_sort_for_node,
    _reader_empty_response,
    _reader_href,
    api_img_proxy,
    build_reader_page,
    get_all_manual_tag_names,
    get_archived_saved_keys,
    get_entry_detail,
    get_feed_display_prefs,
    get_manual_tags_for_entry,
    get_meta_connection,
    get_root_folder_id,
    html_sanitize,
    normalize_search_query,
    normalize_tag_value,
    resolve_reader_article_html,
    resolve_reader_backlog,
    templates,
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


@router.get("/read", response_class=HTMLResponse)
def reader_view(
    request: Request,
    feed_url: str | None = Query(default=None),
    entry_id: str | None = Query(default=None),
    folder_id: int | None = Query(default=None),
    list_feed_url: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    archived: str | None = Query(default=None),
    q: str | None = Query(default=None),
    scope: str = Query(default="saved"),
    sort: str | None = Query(default=None),
    resume_sort: str | None = Query(default=None),
    kept: str | None = Query(default=None),
    confirm_delete_tag: str | None = Query(default=None),
):
    """Read Mode. No entry selected -> the 2-pane browse; an entry selected ->
    the full-screen paginated reader. Two scopes: ``saved`` (the starred backlog;
    Archive is the done-axis) and ``feeds`` (ordinary unread feed reading, with a
    feeds tree drilling to individual feeds).

    *sort* is one of `_READ_SORTS` and carries through every reader link, so the
    order you browse in is the order Next/Prev walk. The backlog has always taken
    sort_by/sort_dir; Read Mode simply pinned them to newest-first and gave no way
    to change it, which is wrong for a comic backlog you read oldest-first."""
    _ua = (request.headers.get("user-agent") or "").strip()
    if _ua and _ua not in _READ_MODE_UA_SEEN and len(_READ_MODE_UA_SEEN) < 50:
        _READ_MODE_UA_SEEN.add(_ua)
        LOGGER.info("[read-mode-ua] %s", _ua[:300])
    is_feeds = scope == "feeds"
    tag_val = normalize_tag_value(tag)
    q_val = normalize_search_query(q)
    feed_scope = list_feed_url or None
    # Search reaches every saved item; otherwise the inbox, unless ?archived=1.
    archived_view = (not is_feeds) and str(archived) == "1"
    archived_filter = None if (is_feeds or q_val) else archived_view
    # A node is "selected" once the user picks All (root folder), a folder, a feed,
    # a tag, Archive, or a search. A bare /read has no node selected: it lands on
    # the tree only, so we never auto-load the whole (huge) backlog.
    # "All Saved" — everything kept (starred OR tagged) minus archived, i.e. what
    # the main app's Saved view shows. Its own node rather than a mode of the
    # Inbox: the Inbox is the to-do pile and must stay small, but the two modes
    # disagreeing about what exists is exactly the mismatch Read Mode is meant
    # not to have.
    all_saved_view = (not is_feeds) and kept == "all"
    node_selected = folder_id is not None or bool(feed_scope) or bool(tag_val) or archived_view or bool(q_val) or all_saved_view

    # The Inbox opens most-recently-starred; every other node keeps newest-first.
    # An explicit ?sort= always wins, so the switcher still works everywhere.
    with get_meta_connection() as _root_conn:
        _read_root_id = get_root_folder_id(_root_conn)
    is_inbox = (not all_saved_view) and _read_is_inbox_node(folder_id, tag_val, archived_view, q_val, scope, _read_root_id)
    sort_val = _read_sort_for_node(sort, is_inbox=is_inbox)
    if is_feeds and sort_val == "starred":
        # Feed entries mostly carry no star date, so this order would be noise.
        # Reachable only by hand-editing the URL; the switcher never offers it.
        sort_val = _READ_SORT_DEFAULT
    resume_sort_val = resume_sort if resume_sort in _READ_SORTS else None
    _sort_by, _sort_dir = _READ_SORTS[sort_val]

    def _load_backlog(limit: int) -> list[dict]:
        return resolve_reader_backlog(
            folder_id=folder_id,
            list_feed_url=feed_scope,
            read_filter=("unread" if is_feeds else "all"),
            star_only=(not is_feeds),
            tag=tag_val,
            sort_by=_sort_by,
            sort_dir=_sort_dir,
            search_query=q_val,
            archived=archived_filter,
            limit=limit,
            # The Inbox is the to-do pile (starred only); every other saved node
            # — tags, Archive, search — still spans the whole kept set.
            kept_scope=("starred" if is_inbox else "kept"),
        )

    # --- BROWSE: no article selected -> 2-pane tree + list -------------------
    if not (entry_id and feed_url):
        items = _load_backlog(150) if node_selected else []
        if is_feeds:
            context = _build_feeds_mode_context(
                request,
                folder_id=folder_id,
                list_feed_url=feed_scope,
                tag=tag_val,
                q=q_val,
                items=items,
                node_selected=node_selected,
            )
        else:
            context = _build_read_mode_context(
                request,
                folder_id=folder_id,
                tag=tag_val,
                list_feed_url=feed_scope,
                archived=archived_view,
                q=q_val,
                items=items,
                node_selected=node_selected,
                sort=sort_val,
                resume_sort=resume_sort_val,
                all_saved=all_saved_view,
                confirm_delete_tag=confirm_delete_tag,
            )
        return templates.TemplateResponse(
            request,
            "read_mode.html",
            context,
            headers={"Cache-Control": "no-store"},
        )

    # --- READ: an article is selected -> full-screen paginated reader --------
    backlog = _load_backlog(250)

    def _href(rec: dict | None) -> str:
        if not rec:
            return ""
        return _reader_href(
            rec["feed_url"],
            rec["id"],
            folder_id=folder_id,
            tag=tag_val,
            archived=archived_view,
            q=q_val,
            scope=scope,
            list_feed_url=feed_scope,
            sort=sort_val,
            kept_all=all_saved_view,
        )

    current: dict | None = None
    prev_rec: dict | None = None
    next_rec: dict | None = None
    for i, rec in enumerate(backlog):
        if rec["feed_url"] == feed_url and rec["id"] == entry_id:
            current = rec
            prev_rec = backlog[i - 1] if i > 0 else None
            next_rec = backlog[i + 1] if i + 1 < len(backlog) else None
            break
    if current is None:
        # Not in the current node's list (just archived/deleted, or a stale
        # link): still render it; "next" points at the head of what remains.
        detail = get_entry_detail(feed_url, entry_id)
        if detail is None and not backlog:
            return _reader_empty_response()
        current = detail or {"feed_url": feed_url, "id": entry_id, "title": feed_url, "link": ""}
        next_rec = backlog[0] if backlog else None

    cur_feed = str(current["feed_url"])
    cur_id = str(current["id"])
    cur_title = str(current.get("title") or current.get("link") or "(untitled)")
    cur_link = str(current.get("link") or "")

    # Deliberately NOT marked read here. Serving the page only means the article
    # was opened, and in an e-ink browse loop opening is how you find out whether
    # you want to read something — marking on render turned every peek into a
    # read. The reader posts to /entries/read once pagination settles and the
    # last page has actually been reached (static/reader.js), which is the first
    # moment the whole article has been on screen. A one-page article qualifies
    # immediately, because there it is true.

    article_html = resolve_reader_article_html(cur_feed, cur_id, cur_link)
    is_archived = (not is_feeds) and (cur_feed, cur_id) in get_archived_saved_keys()
    with get_meta_connection() as _disp_conn:
        _katex_dollar_math = bool(get_feed_display_prefs(_disp_conn, cur_feed).get("katex_dollar_math", 0))

    return build_reader_page(
        title=cur_title,
        article_html=article_html,
        source_link=cur_link,
        prev_href=_href(prev_rec),
        next_href=_href(next_rec),
        back_href=_read_browse_href(folder_id, tag_val, archived_view, q_val, scope, feed_scope, sort_val),
        feed_url=cur_feed,
        entry_id=cur_id,
        is_archived=is_archived,
        csrf_token=_csrf_token_for(request),
        show_saved_actions=not is_feeds,
        date_display=_read_mode_date(current),
        manual_tags=tuple(get_manual_tags_for_entry(cur_feed, cur_id)),
        katex_dollar_math=_katex_dollar_math,
        all_tag_names=tuple(get_all_manual_tag_names()),
    )
