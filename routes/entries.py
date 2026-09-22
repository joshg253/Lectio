"""Entry-level surface (Plan.md's main.py/index.html breakup, Stage 9 of the
route-by-URL-prefix split -- 45 `/entries/*` routes, scoped into its own A-E
sub-stages so it doesn't land as one huge diff, same reasoning as Stage 8's
`routes/feeds.py`).

**Stage 9A only, so far.** Sub-stages B (entry metadata edits + attachments),
C (move/organize + tags), D (read/unread/star state + integration sends), and
E (`/entries/pane` alone, last) will add to this same module in later tasks --
don't assume this is the final state.

Stage 9A -- content/reading utility, 12 routes: `GET /entries/lead-image`,
`GET /entries/media/audio`, `GET /entries/media/download`,
`POST /entries/thumb-crop`, `GET /entries/readability`, `GET /entries/source`,
`GET /entries/frame-check`, `GET /entries/feed-tags`,
`GET /entries/content/has-original`, `POST /entries/content/clean`,
`POST /entries/content/revert`, `GET /entries/autofetch-status`. No ordering
constraint: none of these handlers touch `_run_automation_after_refresh` or
anything else from the late `services.automation_rules` import, so this
module is imported alongside the plain `routes.compat_*`/`routes.tags`-style
modules.

Only one helper moved with its route, having no caller anywhere else:
`_wrap_readability_html` (with `entry_readability`) -- it wraps a readability
extraction in the reader-view HTML shell, used only by that one route. Its own
neighbor `_resolve_archived_readability_html`, despite sitting right next to
it in main.py, stays and is imported back: it is also called by
`resolve_reader_article_html`, the still-in-main.py e-ink `/read` view's
article resolver (Stage 10), so moving it would have broken that caller.
Everything else these 12 routes touch is pre-existing main.py-resident,
widely-shared infrastructure or exercised directly as `main.<name>` by a
dedicated test file, confirmed via the usual `routes/*.py` + `scripts/*.py` +
`tests/` three-way grep, not just main.py and tests: `build_readability_response`
(tested directly and monkeypatched by several dedicated test files),
`_resolve_entry_audio_url`/`_find_entry_audio_url` (shared between
`/entries/media/audio` and `/entries/media/download`, and
`_resolve_entry_audio_url` has a third still-in-main.py caller besides),
`_lead_image_display_url`/`_resolve_entry_content_html` (each with a second
still-in-main.py caller), `_CLEANUP_ERROR_MESSAGES`/`_CLEANUP_ERROR_FALLBACK`
(tested directly as `main.<name>` by `tests/services/test_content_edits.py`),
`_VALID_THUMB_CROPS` (already re-imported by `routes/feeds.py` since Stage 8C,
for the same constant), `_autofetch_jobs` (a `state.py`-sourced `_PerUserDict`
singleton also written by the still-in-main.py star/tag routes, Stage 9D), and
the whole family of shared services/singletons (`lead_image_service`,
`starred_archive_service`, `saved_articles_service`, `feed_tag_service`,
`feed_tags_service_mod`, `content_edits`, `html_sanitize`, `url_guard`,
`feed_refresh_service`, `get_reader`, `get_meta_connection`).

Two test files needed retargeting for the "handler registered directly as
`main.<name>` on a bare test `FastAPI()` app" gotcha:
`tests/integration/test_autofetch_pane_refresh.py` (-> `routes.entries.entry_autofetch_status`)
and `tests/integration/test_entry_content_cleanup.py` (->
`routes.entries.clean_entry_content_route`/`routes.entries.revert_entry_content_route`).
Two more needed retargeting for the plain-function-call variant of the same
gotcha (a test calling the handler directly rather than through `TestClient`):
`tests/integration/test_feed_tag_dismiss_survives_reharvest.py` and
`tests/integration/test_orphan_entry_tags.py` (three call sites), both ->
`routes.entries.entry_feed_tags_route`. One test hit the copied-reference
monkeypatch gotcha over real HTTP: `tests/integration/test_reader_view_stored_content_fallback.py`
monkeypatches `main.build_readability_response` and then hits
`GET /entries/readability` via `TestClient(main.app)` -- since
`entry_readability` now does its own `from main import build_readability_response`,
the patch needed doubling onto `routes.entries.build_readability_response` too.
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from main import (
    _CLEANUP_ERROR_FALLBACK,
    _CLEANUP_ERROR_MESSAGES,
    _READER_VIEW_MEDIA_CSS,
    _VALID_THUMB_CROPS,
    LOGGER,
    READABILITY_USER_AGENT,
    _autofetch_jobs,
    _find_entry_audio_url,
    _get_orphan_manual_tags,
    _lead_image_display_url,
    _resolve_archived_readability_html,
    _resolve_entry_audio_url,
    _resolve_entry_content_html,
    _strip_bandcamp_track_signature,
    build_readability_response,
    build_source_proxy_response,
    content_edits,
    feed_refresh_service,
    feed_tag_service,
    feed_tags_service_mod,
    get_entry_detail,
    get_feed_display_prefs,
    get_feed_pinned_tags,
    get_feed_tag_filter_rule,
    get_feed_tag_suggestions,
    get_manual_tags_for_resource,
    get_meta_connection,
    get_reader,
    html_sanitize,
    lead_image_service,
    normalize_tag_value,
    parse_tag_filter_spec,
    probe_frameability,
    saved_articles_service,
    starred_archive_service,
    url_guard,
)

router = APIRouter()


@router.get("/entries/lead-image")
def entry_lead_image_status(feed_url: str, entry_id: str):
    """Lightweight polling endpoint for background lead-image fetch status.

    Returns {"status": "pending"|"none"|"ready", "url": str|null}.
    """
    key = (feed_url, entry_id)
    cached = lead_image_service._cache.get(key, "ABSENT")
    in_progress = key in lead_image_service._source_fetch_in_progress
    if cached != "ABSENT" and cached is not None:
        # The rule can only ever rewrite a query parameter, so a URL without a
        # query cannot be affected and does not need the lookup. This branch is
        # the terminal one — polling stops once it returns "ready" — so the read
        # is at most one per entry, not one per poll.
        _rule = None
        if "?" in cached:
            with get_meta_connection() as _conn:
                _rule = get_feed_display_prefs(_conn, feed_url).get("image_size_rule")
        display_url = _lead_image_display_url(cached, _rule)
        return JSONResponse({"status": "ready", "url": display_url})
    if in_progress:
        return JSONResponse({"status": "pending", "url": None})
    return JSONResponse({"status": "none", "url": None})


@router.get("/entries/media/audio")
def media_audio_redirect(feed_url: str, entry_id: str):
    """Redirect to the entry's audio enclosure URL.

    If the stored URL returns a non-2xx response (e.g. Patreon signed URLs
    expire after ~24 h), the feed is refreshed once to obtain a fresh URL
    before redirecting.
    """
    with get_reader() as reader:
        entry = reader.get_entry((feed_url, entry_id), None)
        if not entry:
            raise HTTPException(status_code=404, detail="Entry not found")
        with get_meta_connection() as _mconn:
            audio_url = _resolve_entry_audio_url(_mconn, feed_url, entry_id, entry)
        if not audio_url:
            raise HTTPException(status_code=404, detail="No audio enclosure found")

        # Quick validity check; refresh the feed if the URL is expired.
        try:
            head = httpx.head(
                audio_url,
                follow_redirects=True,
                timeout=4.0,
                headers={"User-Agent": READABILITY_USER_AGENT},
            )
            if head.status_code not in (200, 206):
                try:
                    feed_refresh_service.update_feeds([feed_url])
                    fresh = reader.get_entry((feed_url, entry_id), None)
                    if fresh:
                        fresh_url = _find_entry_audio_url(fresh)
                        if fresh_url:
                            audio_url = fresh_url
                except Exception:
                    LOGGER.warning("Audio URL refresh failed for %s", feed_url, exc_info=True)
        except Exception:
            pass  # Network error on HEAD — try the stored URL anyway

    return RedirectResponse(audio_url, status_code=302)


@router.get("/entries/media/download")
def media_audio_download(feed_url: str, entry_id: str):
    """Proxy the entry's audio enclosure as an attachment download.

    Handles expired signed URLs the same way as /entries/media/audio.
    Uses a streaming proxy so the file is downloaded through the server,
    which avoids cross-origin restrictions on the browser download attribute.
    """
    with get_reader() as reader:
        entry = reader.get_entry((feed_url, entry_id), None)
        if not entry:
            raise HTTPException(status_code=404, detail="Entry not found")
        with get_meta_connection() as _mconn:
            audio_url = _resolve_entry_audio_url(_mconn, feed_url, entry_id, entry)
        if not audio_url:
            raise HTTPException(status_code=404, detail="No audio enclosure found")
        entry_title = str(entry.title or "audio")

        # Refresh if expired.
        try:
            head = httpx.head(
                audio_url,
                follow_redirects=True,
                timeout=4.0,
                headers={"User-Agent": READABILITY_USER_AGENT},
            )
            if head.status_code not in (200, 206):
                try:
                    feed_refresh_service.update_feeds([feed_url])
                    fresh = reader.get_entry((feed_url, entry_id), None)
                    if fresh:
                        fresh_url = _find_entry_audio_url(fresh)
                        if fresh_url:
                            audio_url = fresh_url
                except Exception:
                    LOGGER.warning("Audio URL refresh failed for %s", feed_url, exc_info=True)
        except Exception:
            pass

    # Derive a clean filename from the URL path, falling back to entry title.
    parsed_path = urlparse(audio_url).path.rstrip("/").split("/")[-1]
    if parsed_path and "." in parsed_path:
        filename = re.sub(r"[^\w.\-]", "_", parsed_path)
    else:
        safe_title = re.sub(r"[^\w\- ]", "", entry_title).strip()[:80] or "audio"
        filename = safe_title.replace(" ", "_") + ".mp3"

    def _stream():
        with url_guard.build_client(
            follow_redirects=True,
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0),
            headers={"User-Agent": READABILITY_USER_AGENT},
        ) as client:
            with client.stream("GET", audio_url) as resp:
                resp.raise_for_status()
                for chunk in resp.iter_bytes(chunk_size=65536):
                    yield chunk

    return StreamingResponse(
        _stream(),
        media_type="audio/mpeg",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/entries/thumb-crop")
def set_entry_thumb_crop_route(
    feed_url: str = Form(...),
    entry_id: str = Form(...),
    crop: str = Form(default=""),
):
    """Save (or clear) a per-entry thumbnail crop override."""
    effective = crop.strip() if crop.strip() in _VALID_THUMB_CROPS else None
    lead_image_service.store_entry_thumb_crop(feed_url, entry_id, effective)
    return JSONResponse({"ok": True, "crop": effective})


@router.get("/entries/readability")
def entry_readability(
    url: str,
    feed_url: str | None = Query(default=None),
    entry_id: str | None = Query(default=None),
):
    # If this entry is starred and a complete archive exists, serve the
    # archived readability HTML so the view stays available even if the
    # source is gone. Otherwise fall through to the live extractor.
    archived_html = _resolve_archived_readability_html(feed_url, entry_id)
    if archived_html:
        return _wrap_readability_html(archived_html, url)
    # A complete archive with no readability copy (the recapture mismatch
    # guard refused a parked/replaced page, or the live fetch failed outright)
    # means capture already tried once -- the entry's own stored content is a
    # safer bet than a second live fetch of a page that has already shown
    # itself untrustworthy or unreachable. Scoped to kept entries specifically:
    # an ordinary entry with no archive at all must still reach the live
    # fetch below, which is what recovers a full article from a thin RSS
    # stub in the common case.
    if feed_url and entry_id and starred_archive_service.has_complete_archive(feed_url, entry_id):
        detail = get_entry_detail(feed_url, entry_id)
        stored = str((detail or {}).get("content_html") or "")
        if stored:
            return _wrap_readability_html(stored, url)
    return build_readability_response(url)


def _wrap_readability_html(article_html: str, source_url: str) -> HTMLResponse:
    escaped_source = html.escape(source_url)
    article_html = _strip_bandcamp_track_signature(article_html)
    return HTMLResponse(
        (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<title>Reader view</title>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<style>body{margin:0;background:#f6f8fb;color:#1a2430;font-family:Georgia,serif;}"
            "main{max-width:760px;margin:0 auto;padding:1.2rem 1rem 2rem;}"
            "header{font-family:Segoe UI,Arial,sans-serif;margin-bottom:1rem;padding-bottom:.75rem;border-bottom:1px solid #d4dbe5;}"
            "a{color:#0a5ca4;}article{font-size:1.05rem;line-height:1.7;}" + _READER_VIEW_MEDIA_CSS + "article pre{white-space:pre-wrap;}"
            "article *{color:inherit !important;background-color:transparent !important;}"
            "</style></head>"
            f"<body><main><header>"
            f"<a href='{escaped_source}' target='_blank' rel='noopener noreferrer'>Open original</a>"
            "</header>"
            f"<article>{article_html}</article></main></body></html>"
        ),
        status_code=200,
    )


@router.get("/entries/source")
def entry_source(url: str):
    return build_source_proxy_response(url)


@router.get("/entries/frame-check")
def entry_frame_check(url: str):
    return JSONResponse(probe_frameability(url))


@router.get("/entries/feed-tags")
def entry_feed_tags_route(
    feed_url: str = Query(...),
    entry_id: str = Query(...),
):
    """Late chip delivery: the entry pane calls this after render when it has
    no feed-tag chips. Waits briefly for the background source-page fetch the
    entry-open queued (whose sink persists harvested tags), then returns the
    normalized suggestions + filter-rule signs so the client can inject the
    [ + tag ▲ ▼ ] chips into the already-open pane.

    An orphan entry (feed unsubscribed) has no reader resource for
    get_feed_tag_suggestions/get_manual_tags_for_resource to read, and no
    live page to fetch publisher tags from — but it still has manual tags
    (orphan_entry_tags) and pinned/suggested tags (feed_display_prefs, same
    as a live feed). Without this branch, saving a new suggested tag from
    Feed Properties on an already-open orphan entry called this route (see
    submitFeedPropSuggestedTags) and got a 404, so the chip never appeared
    without a full pane reopen — same bug class as _build_orphan_entry_detail
    previously hardcoding feed_tag_suggestions=[].
    """
    with get_reader() as reader:
        entry = reader.get_entry((feed_url, entry_id), None)
        if entry is not None:
            entry_link = str(entry.link or "")
            manual_tags = get_manual_tags_for_resource(reader, entry.resource_id)

    if entry is None:
        if starred_archive_service.get_orphan_feed_title(feed_url) is None:
            return JSONResponse({"error": "unknown entry"}, status_code=404)
        entry_link = ""
        manual_tags = _get_orphan_manual_tags(feed_url, entry_id)
        raw_tags: list[str] = []
    else:
        raw_tags = get_feed_tag_suggestions(feed_url, entry_id)
        if not raw_tags and entry_link and url_guard.is_safe_outbound_url(entry_link):
            # Wait for the fetch queued by the entry-open handler (returns
            # immediately when it already finished or none is in flight).
            lead_image_service.wait_for_source_html_fetch(entry_link, timeout=8.0)
            raw_tags = get_feed_tag_suggestions(feed_url, entry_id)
            if not raw_tags:
                # Fetch finished before the sink existed or raced it — harvest
                # directly from the cached page as a last resort.
                cached = lead_image_service.get_cached_source_html(entry_link)
                if cached is not None:
                    page_tags = feed_tags_service_mod.extract_page_tags(cached[1], entry_link)
                    if page_tags:
                        feed_tag_service.record_entry_tags(feed_url, [(entry_id, page_tags)])
                        # Re-derive dismissal-aware, same reason as the pane build.
                        raw_tags = get_feed_tag_suggestions(feed_url, entry_id)

    # The user's pinned tags go FIRST — they are the ones being reached for, and
    # a feed that ships 28 tags a post would otherwise bury them past the
    # collapse. Prepending rather than a separate list means the existing
    # dedupe below is also what guarantees "never show a chip twice": a pinned
    # tag the publisher happens to ship too appears once, in the pinned position.
    pinned = get_feed_pinned_tags(feed_url)
    _publisher = {n for n in (normalize_tag_value(t) for t in raw_tags) if n}
    # See the entry-pane build for why these two lists exist: a pinned tag the
    # publisher never ships gets no filter arrows, and disappears from the
    # suggestions once it has actually been applied.
    pinned_only = [t for t in pinned if t not in _publisher]
    _manual_now = {normalize_tag_value(t) for t in manual_tags}
    tags: list[str] = []
    for raw_tag in [*pinned, *raw_tags]:
        normalized = normalize_tag_value(raw_tag)
        if not normalized or normalized in tags:
            continue
        if normalized in pinned_only and normalized in _manual_now:
            continue
        tags.append(normalized)

    signs: dict[str, str] = {}
    if tags:
        with get_meta_connection() as conn:
            rule = get_feed_tag_filter_rule(conn, feed_url)
        if rule:
            _req, _good, _exc = parse_tag_filter_spec(str(rule["keyword"] or ""))
            signs = {t: "+" for t in (_req | _good)} | {t: "-" for t in _exc}

    return JSONResponse(
        {
            "ok": True,
            "tags": tags,
            "signs": signs,
            # Which of them are the user's own pinned tags, so the client can mark
            # them: they are a different KIND of suggestion (a standing decision
            # about the feed, not something the publisher said about this post).
            "pinned": pinned,
            "pinned_only": pinned_only,
            "manual_tags": [normalize_tag_value(t) for t in manual_tags],
        }
    )


@router.post("/entries/content/clean")
def clean_entry_content_route(
    feed_url: str = Form(...),
    entry_id: str = Form(...),
    ops: str = Form(...),
):
    """Apply the reading pane's Aardvark-style cleanup to a post's stored body.

    The browser sends *what it removed* (an ordered op list of structural paths
    + fingerprints), not the edited HTML — see services/content_edits for why.
    The ops are replayed here against reader's stored content, the result is
    sanitized and written back through the same path a content re-fetch uses
    (`replace_entry_content`, with `pin_content` so the next refresh can't
    re-serve the junk), and the pristine body is snapshotted first so
    /entries/content/revert can put it back.

    Ops that match nothing are reported rather than guessed at: a rendered node
    that isn't in the stored body (an injected embed, something a render-time
    cleanup already removed) simply has nothing to delete.
    """
    with get_reader() as reader:
        entry = reader.get_entry((feed_url, entry_id), None)
        if entry is None:
            return JSONResponse({"ok": False, "error": "Entry not found."}, status_code=404)
        try:
            parsed_ops = content_edits.parse_ops(ops)
            content_html = _resolve_entry_content_html(entry)
            new_html, applied, unmatched = content_edits.apply_ops(content_html, parsed_ops)
        except content_edits.ContentEditError as exc:
            # The wording lives here, keyed by the error's own code, so nothing
            # derived from an exception object reaches the response — the
            # dataflow behind py/stack-trace-exposure does not exist rather than
            # being argued about. The exception's message still goes to the log.
            LOGGER.info("[cleanup] refused for %s: %s", entry_id, exc)
            return JSONResponse(
                {"ok": False, "error": _CLEANUP_ERROR_MESSAGES.get(getattr(exc, "code", ""), _CLEANUP_ERROR_FALLBACK)},
                status_code=400,
            )
        if not applied:
            return JSONResponse(
                {"ok": False, "error": "None of those elements could be matched in the stored article.", "unmatched": unmatched},
                status_code=409,
            )
        # The result is user-directed but still passes the normal allowlist —
        # a cleanup must never be a way to widen what the body may contain.
        new_html = html_sanitize.sanitize_html(new_html)
        original_content = saved_articles_service.read_entry_content_json(reader, feed_url, entry_id)
        with get_meta_connection() as conn:
            if original_content is not None:
                # First edit only: repeated cleanups must still revert to the
                # true original rather than to the previous cleanup's output.
                conn.execute(
                    "INSERT OR IGNORE INTO entry_content_edits"
                    " (feed_url, entry_id, original_content, ops, edited_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (feed_url, entry_id, original_content, "[]", datetime.now(timezone.utc).isoformat()),
                )
            existing = conn.execute(
                "SELECT ops FROM entry_content_edits WHERE feed_url = ? AND entry_id = ?",
                (feed_url, entry_id),
            ).fetchone()
            prior_ops = []
            if existing:
                try:
                    prior_ops = json.loads(existing[0]) or []
                except TypeError, ValueError:
                    prior_ops = []
            conn.execute(
                "UPDATE entry_content_edits SET ops = ?, edited_at = ? WHERE feed_url = ? AND entry_id = ?",
                (json.dumps(prior_ops + parsed_ops), datetime.now(timezone.utc).isoformat(), feed_url, entry_id),
            )
            conn.commit()
            saved_articles_service.replace_entry_content(
                reader,
                conn,
                entry_id,
                "",
                new_html,
                feed_url=feed_url,
                bump_received=False,
                pin_content=True,
            )
    return JSONResponse({"ok": True, "applied": applied, "unmatched": unmatched})


@router.get("/entries/content/has-original")
def entry_has_original_content_route(feed_url: str = Query(...), entry_id: str = Query(...)):
    """Whether this post has a stored original to restore.

    Asked per post so the menu can offer Restore only when it would do
    something — a dead control is worse than an absent one, and this is the
    recovery path for a re-fetch that replaced an article, so it has to be
    trustworthy when it does appear.
    """
    try:
        with get_meta_connection() as conn:
            found = conn.execute(
                "SELECT 1 FROM entry_content_edits WHERE feed_url = ? AND entry_id = ?",
                (feed_url, entry_id),
            ).fetchone()
    except sqlite3.OperationalError:
        found = None  # tenant DB predates the table
    return JSONResponse({"ok": True, "has_original": bool(found)})


@router.post("/entries/content/revert")
def revert_entry_content_route(feed_url: str = Form(...), entry_id: str = Form(...)):
    """Undo every cleanup on a post, restoring the body as the feed served it."""
    with get_meta_connection() as conn:
        row = conn.execute(
            "SELECT original_content FROM entry_content_edits WHERE feed_url = ? AND entry_id = ?",
            (feed_url, entry_id),
        ).fetchone()
        if row is None:
            return JSONResponse({"ok": False, "error": "This post has no cleanup to revert."}, status_code=404)
        with get_reader() as reader:
            if reader.get_entry((feed_url, entry_id), None) is None:
                return JSONResponse({"ok": False, "error": "Entry not found."}, status_code=404)
            saved_articles_service.restore_entry_content(reader, feed_url, entry_id, row[0])
        # Drop the pin too, or the refresh service would re-apply the cleaned
        # copy over the body we just restored.
        conn.execute(
            "DELETE FROM entry_content_overrides WHERE feed_url = ? AND entry_id = ?",
            (feed_url, entry_id),
        )
        conn.execute(
            "DELETE FROM entry_content_edits WHERE feed_url = ? AND entry_id = ?",
            (feed_url, entry_id),
        )
        conn.commit()
    return JSONResponse({"ok": True})


@router.get("/entries/autofetch-status")
def entry_autofetch_status(feed_url: str = Query(...), entry_id: str = Query(...)):
    """Poll target for a pane whose star/tag response flagged autofetch_pending —
    reports whether _maybe_autofetch_on_keep's background re-fetch for THIS entry
    is still running, and if not, whether it actually found a fuller copy."""
    job = _autofetch_jobs.get((feed_url, entry_id))
    if job is None:
        return JSONResponse({"ok": True, "pending": False, "done": False, "success": None})
    return JSONResponse({"ok": True, "pending": bool(job.get("running")), "done": not job.get("running"), "success": job.get("ok")})
