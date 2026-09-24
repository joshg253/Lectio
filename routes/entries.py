"""Entry-level surface (Plan.md's main.py/index.html breakup, Stage 9 of the
route-by-URL-prefix split -- 45 `/entries/*` routes, scoped into its own A-E
sub-stages so it doesn't land as one huge diff, same reasoning as Stage 8's
`routes/feeds.py`).

**Stage 9 is now COMPLETE.** Sub-stages A (content/reading utility), B (entry
metadata edits + attachments), C (move/organize + tags), D (read/unread/star
state + integration sends), and E (`/entries/pane` alone) have all landed in
this module -- 45 routes total, no further sub-stages planned. B/C/D's own
per-stage notes live only in `routes/__init__.py`'s docstring (not narrated
here); this file's notes below cover 9A in detail, then jump to 9E.

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

Stage 9E -- `GET /entries/pane` alone (1 route, the last sub-stage). `entry_pane` is pure orchestration: param
normalization, one `get_entry_detail` call for the selected entry, a small feed_url->folder_id map, a few
integration-configured checks, and a `templates.TemplateResponse` render -- it does not call `_home_inner`,
`list_entries_for_feeds`, or `build_reader_page`, the other shared rendering-core functions Plan.md's Landmines
note flags; `get_entry_detail` is the only one it touches, and it (along with the other three) stays in main.py,
imported back. `_get_email_to_default` moved with the route (no other caller, no dedicated test).
`_mark_entry_read_background` looked single-route-only by the same grep but stayed in main.py and got imported
back instead: `tests/integration/test_reader_view.py` monkeypatches `main._mark_entry_read_background` as a
defensive stub for the unrelated `reader_view` (`/read`) route, which doesn't actually call it -- moving the
function out of main.py would still have broken that `setattr` (it requires the attribute to exist), so it was
left in place rather than touching an unrelated test for a route this stage didn't move.
`normalize_resume_read_filter`/`unsubscribed_feed_urls_among` stayed too, confirmed shared with `_home_inner`.
`get_meta_structure_snapshot`, `is_instapaper_configured`, `is_quire_configured`, `pinterest_oauth_connected`,
`reddit_connected`, and `templates` are pre-existing widely-shared main.py infrastructure -- none of it moved.
No `services.automation_rules` ordering constraint. One test file retargeted for the "handler registered
directly as `main.<name>` on a bare test `FastAPI()` app" gotcha: `tests/integration/test_hide_locked_comics.py`
(-> `routes.entries.entry_pane`). No `scripts/*.py` callers. See `routes/__init__.py`'s docstring for the fuller
write-up.
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import cast
from urllib.parse import quote_plus, urlparse

import httpx
from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from main import (
    _CLEANUP_ERROR_FALLBACK,
    _CLEANUP_ERROR_MESSAGES,
    _ENTRY_LINK_MAX_LEN,
    _MOVE_BATCH_CAP,
    _RANGE_READ_LIMIT,
    _READER_VIEW_MEDIA_CSS,
    _VALID_THUMB_CROPS,
    _archived_copy_is_plausible,
    _reader_copy_is_richer,
    EMAIL_TO_SETTING_KEY,
    LOGGER,
    MAX_MANUAL_TAGS,
    PROFILE_EMAIL_SETTING_KEY,
    READABILITY_USER_AGENT,
    SETTING_INSTAPAPER_PASSWORD,
    SETTING_INSTAPAPER_USERNAME,
    STARRED_ASSET_URL_PREFIX,
    _autofetch_jobs,
    _bump_unread_counts_generation,
    _entry_query_suffix,
    _filtered_file_enclosures,
    _find_entry_audio_url,
    _get_orphan_manual_tags,
    _hard_delete_entry,
    _instapaper_save_url,
    _lead_image_display_url,
    _mark_entries_as_read_for_view,
    _mark_entry_read_background,
    _maybe_autofetch_on_keep,
    _move_entry_to_feed,
    _prune_entries,
    _quire_add_entry,
    _resolve_archived_readability_html,
    _resolve_entry_audio_url,
    _resolve_entry_content_html,
    _run_in_user_context,
    _sanitize_html_allowlist,
    _strip_bandcamp_track_signature,
    _undo_token_problem,
    _youtube_unpremiered_video_id,
    append_read_history,
    apply_manual_tag_edits,
    apply_star_state,
    build_read_filter_query,
    build_readability_response,
    build_resume_read_filter_query,
    build_sort_query,
    build_source_proxy_response,
    build_star_only_query,
    candidate_attachment_links_in_html,
    content_edits,
    delete_entry_read_state,
    entry_effective_date,
    feed_refresh_service,
    feed_tag_service,
    feed_tags_service_mod,
    filter_feed_urls,
    get_all_reader_feed_urls,
    get_disabled_feed_urls,
    get_entry_detail,
    get_feed_display_prefs,
    get_feed_pinned_tags,
    get_feed_tag_filter_rule,
    get_feed_tag_suggestions,
    get_folder_feed_urls,
    get_kept_feed_urls,
    get_manual_tags_for_entry,
    get_manual_tags_for_resource,
    get_meta_connection,
    get_meta_structure_snapshot,
    get_quire_usage_status,
    get_quire_user_token,
    get_reader,
    get_resend_api_key,
    get_resend_from,
    get_root_folder_id,
    get_runtime_setting,
    get_setting,
    get_starred_archive_connection,
    get_tagged_entry_keys,
    get_yt_folder_name,
    html_sanitize,
    invalidate_unread_counts_cache,
    is_async_action_request,
    is_email_configured,
    is_instapaper_configured,
    is_quire_configured,
    is_quire_connected,
    lead_image_service,
    list_entries_for_feeds,
    mark_entry_read_everywhere,
    merge_orphan_saved_entries,
    normalize_read_filter,
    normalize_resume_read_filter,
    normalize_search_query,
    normalize_sort_by,
    normalize_sort_dir,
    normalize_star_only,
    normalize_tag_value,
    parse_manual_hashtags,
    parse_manual_tag_edit_tokens,
    parse_tag_filter_spec,
    pinterest_oauth_connected,
    probe_frameability,
    quire_project_oid,
    reddit_connected,
    saved_articles_service,
    search_terms_from_query,
    send_article_email,
    set_entry_archived,
    set_manual_tags_for_entry,
    starred_archive_service,
    templates,
    unread_counts_cache,
    unread_counts_cache_lock,
    unsubscribed_feed_urls_among,
    upsert_entry_read_state,
    url_guard,
    fetch_readability_article,
)
from services import tenancy

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


@router.post("/entries/delete")
def delete_entry_route(feed_url: str = Form(...), entry_id: str = Form(...)):
    """Hard-delete a single entry (spam, corrupted post) and tombstone it."""
    with get_reader() as reader:
        entry = reader.get_entry((feed_url, entry_id), None)
        if entry is None:
            return JSONResponse({"ok": False, "error": "Entry not found."}, status_code=404)
        try:
            _hard_delete_entry(reader, feed_url, entry_id, entry)
        except Exception:  # noqa: BLE001
            LOGGER.exception("[delete-entry] failed for %s in %s", entry_id, feed_url)
            return JSONResponse({"ok": False, "error": "Delete failed — see server logs."}, status_code=500)
    invalidate_unread_counts_cache()
    return JSONResponse({"ok": True})


def _parse_local_date_to_utc(published: str) -> datetime | None:
    """A date from the picker, read as LOCAL time, returned as UTC.

    Naive input means midnight *where the user is*; treating it as UTC rendered
    it back through astimezone() as the previous day. See the note in
    set_entry_date_route, which this was factored out of.
    """
    try:
        dt = datetime.fromisoformat(published)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc)


def _set_orphan_entry_date(feed_url: str, entry_id: str, published: str) -> JSONResponse:
    """Set (or clear) the date of a saved entry whose feed is gone.

    There is no reader row to update, so the date is written where the orphan
    actually reads it from: ``archived_entry.published_at``, a float epoch. The
    override row is kept in step so the two agree if the feed is ever
    re-subscribed and the entry comes back to reader.
    """
    try:
        _arch = get_starred_archive_connection()
    except Exception:  # noqa: BLE001 — no archive means no orphan can exist
        return JSONResponse({"ok": False, "error": "Entry not found."}, status_code=404)
    # Not archive_conn(): the guard above has to see a connect failure before
    # the body runs. closing() then commits-and-closes the same way it does.
    with closing(_arch), _arch as arch:
        try:
            row = arch.execute(
                "SELECT 1 FROM archived_entry WHERE feed_url = ? AND entry_id = ?",
                (feed_url, entry_id),
            ).fetchone()
        except sqlite3.OperationalError:
            # Archive not provisioned yet (fresh install): the entry is in
            # neither store, which is the same answer as not finding it.
            row = None
        if row is None:
            return JSONResponse({"ok": False, "error": "Entry not found."}, status_code=404)
        if not published:
            arch.execute(
                "UPDATE archived_entry SET published_at = NULL WHERE feed_url = ? AND entry_id = ?",
                (feed_url, entry_id),
            )
        else:
            dt = _parse_local_date_to_utc(published)
            if dt is None:
                return JSONResponse({"ok": False, "error": "Invalid date."}, status_code=400)
            arch.execute(
                "UPDATE archived_entry SET published_at = ? WHERE feed_url = ? AND entry_id = ?",
                (dt.timestamp(), feed_url, entry_id),
            )
    with get_meta_connection() as conn:
        if not published:
            conn.execute(
                "DELETE FROM entry_date_overrides WHERE feed_url = ? AND entry_id = ?",
                (feed_url, entry_id),
            )
        else:
            # dt is the one parsed above — validated non-None there (an invalid
            # date already returned 400 before this block), and published hasn't
            # changed since, so re-parsing it here would just repeat that work.
            conn.execute(
                "INSERT OR REPLACE INTO entry_date_overrides (feed_url, entry_id, published, source) VALUES (?, ?, ?, 'manual')",
                (feed_url, entry_id, dt.strftime("%Y-%m-%d %H:%M:%S")),
            )
    invalidate_unread_counts_cache()
    return JSONResponse({"ok": True, "orphan": True, "cleared": not published})


@router.post("/entries/set-date")
def set_entry_date_route(feed_url: str = Form(...), entry_id: str = Form(...), published: str = Form("")):
    """Override a post's published date (fixes garbage dates — epoch-0 entries
    sort to the bottom forever).

    reader's EntryData is ingest-owned with no public setter, and the list sort
    happens in SQL on reader's `published` column — so the corrected date is
    written straight into that column, and an override row in the meta DB lets
    the refresh service re-pin it if a refresh re-ingests the feed's original
    (garbage) value. An empty `published` clears the override (the current
    stored date is left as-is until the feed next updates the entry).
    """
    published = published.strip()
    with get_reader() as reader:
        entry_missing = reader.get_entry((feed_url, entry_id), None) is None
    if entry_missing:
        # An ORPHAN save: its feed was unsubscribed, so the entry exists only in
        # the archive. Saved still lists it (merge_orphan_saved_entries) and its
        # date comes from archived_entry.published_at — so refusing here was a
        # dead end with no other way to correct the date. Reported on a
        # feedburner save whose feed is long gone.
        return _set_orphan_entry_date(feed_url, entry_id, published)
    with get_reader() as reader:
        if not published:
            with get_meta_connection() as conn:
                conn.execute(
                    "DELETE FROM entry_date_overrides WHERE feed_url = ? AND entry_id = ?",
                    (feed_url, entry_id),
                )
            return JSONResponse({"ok": True, "cleared": True})
        try:
            dt = datetime.fromisoformat(published)
        except ValueError:
            return JSONResponse({"ok": False, "error": "Invalid date."}, status_code=400)
        # A naive input is LOCAL time, not UTC. "2023-07-06" from the date picker
        # means midnight where the user is; storing it straight into reader's
        # naive-UTC column made it midnight UTC, which then rendered through
        # format_datetime_for_ui's astimezone() as "Jul 5, 2023 5pm" — the day
        # before. Reported after setting a date to 7/6/23.
        if dt.tzinfo is None:
            dt = dt.astimezone()  # attaches the local zone (naive == local)
        stored = dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with get_meta_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO entry_date_overrides (feed_url, entry_id, published, source) VALUES (?, ?, ?, 'manual')",
                (feed_url, entry_id, stored),
            )
        db = reader._storage.get_db()
        db.execute(
            "UPDATE entries SET published = ? WHERE feed = ? AND id = ?",
            (stored, feed_url, entry_id),
        )
        db.commit()
    invalidate_unread_counts_cache()
    return JSONResponse({"ok": True, "published": stored})


_ENTRY_TITLE_MAX_LEN = 500


@router.post("/entries/set-title")
def set_entry_title_route(feed_url: str = Form(...), entry_id: str = Form(...), title: str = Form("")):
    """Override a post's title (fixes "(untitled)" posts, garbage feed titles,
    and lets saved articles be renamed).

    Same mechanism as /entries/set-date: reader's EntryData is ingest-owned, so
    the corrected title is written straight into reader's `entries.title`
    column, and a meta override row lets the refresh service re-pin it if a
    refresh re-ingests the feed's original value. An empty `title` clears the
    override (the stored title is left as-is until the feed next updates the
    entry).
    """
    title = title.strip()
    with get_reader() as reader:
        if reader.get_entry((feed_url, entry_id), None) is None:
            return JSONResponse({"ok": False, "error": "Entry not found."}, status_code=404)
        if not title:
            with get_meta_connection() as conn:
                conn.execute(
                    "DELETE FROM entry_title_overrides WHERE feed_url = ? AND entry_id = ?",
                    (feed_url, entry_id),
                )
            return JSONResponse({"ok": True, "cleared": True})
        if len(title) > _ENTRY_TITLE_MAX_LEN:
            return JSONResponse({"ok": False, "error": "Title is too long."}, status_code=400)
        with get_meta_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO entry_title_overrides (feed_url, entry_id, title) VALUES (?, ?, ?)",
                (feed_url, entry_id, title),
            )
        db = reader._storage.get_db()
        db.execute(
            "UPDATE entries SET title = ? WHERE feed = ? AND id = ?",
            (title, feed_url, entry_id),
        )
        db.commit()
    return JSONResponse({"ok": True, "title": title})


@router.post("/entries/set-link")
def set_entry_link_route(feed_url: str = Form(...), entry_id: str = Form(...), link: str = Form("")):
    """Override a post's source URL (repairs a dead or moved link).

    Same mechanism as /entries/set-title: the corrected link is written into
    reader's `entries.link` column and a meta `entry_link_overrides` row lets
    the refresh service re-pin it if the feed re-ingests the original. An empty
    `link` clears the override.

    **Only `link` changes — never the entry id.** For a Lectio capture the id
    *is* the original URL, and it keys the star row, manual tags, and archive
    rows; re-keying would scatter all three. Changing the link alone is enough:
    the UI's "open original" and the Re-fetch path both read `link` first, so
    correcting it here is what lets Re-fetch pull the article from its new home.

    This is the manual counterpart to the archive worker's automatic
    canonicalization (`_apply_canonical_entry_link`), which only fires for
    redirector links it can resolve. Dead redirectors like feedproxy.google.com
    can't be resolved by anything — the user finds the new location by hand and
    pins it here.
    """
    link = link.strip()
    with get_reader() as reader:
        if reader.get_entry((feed_url, entry_id), None) is None:
            return JSONResponse({"ok": False, "error": "Entry not found."}, status_code=404)
        if not link:
            with get_meta_connection() as conn:
                conn.execute(
                    "DELETE FROM entry_link_overrides WHERE feed_url = ? AND entry_id = ?",
                    (feed_url, entry_id),
                )
                conn.commit()
            return JSONResponse({"ok": True, "cleared": True})
        if len(link) > _ENTRY_LINK_MAX_LEN:
            return JSONResponse({"ok": False, "error": "URL is too long."}, status_code=400)
        # http(s) only. safe_link_url also passes mailto:/tel:, which are fine
        # as hrefs but are not source URLs a re-fetch could ever follow.
        parsed = urlparse(link)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return JSONResponse({"ok": False, "error": "Enter a valid http(s) URL."}, status_code=400)
        if not html_sanitize.safe_link_url(link):
            return JSONResponse({"ok": False, "error": "That URL isn't safe to link."}, status_code=400)
        with get_meta_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO entry_link_overrides (feed_url, entry_id, link) VALUES (?, ?, ?)",
                (feed_url, entry_id, link),
            )
            conn.commit()
        db = reader._storage.get_db()
        db.execute(
            "UPDATE entries SET link = ? WHERE feed = ? AND id = ?",
            (link, feed_url, entry_id),
        )
        db.commit()
    return JSONResponse({"ok": True, "link": link})


def _entry_content_html_and_base(feed_url: str, entry_id: str) -> tuple[str, str, list[str]]:
    """Content HTML + base URL + file-enclosure URLs for the per-entry
    Attachments panel's candidate scan -- the same content source a live
    entry's pane already renders from, or (for an orphan whose feed is gone)
    the readability capture from the archive. Never the raw fetched page:
    that's what the 2026-09-12 image-scope fix stopped scanning for exactly
    this reason (page chrome, not the article).

    Enclosure URLs are returned alongside content_html because a magazine's
    issue PDF/EPUB or a devblog's demo video are routinely declared ONLY as
    an ``<enclosure>`` -- never linked anywhere in the body -- so scanning
    content_html alone misses them entirely (see _filtered_file_enclosures).
    An orphan has no enclosure data preserved in the archive, so this is
    always [] for that branch.
    """
    with get_reader() as reader:
        entry = reader.get_entry((feed_url, entry_id), None)
    if entry is not None:
        content_html = _resolve_entry_content_html(entry) or ""
        base_url = str(getattr(entry, "link", None) or entry_id)
        # No audio_url passed through here (unlike _render_entry_attachments):
        # _filtered_file_enclosures already excludes anything typed audio/* or
        # audio-extensioned on its own, and resolving the exact playing URL
        # needs a meta connection and can enqueue a background media scan as
        # a side effect -- more than a read-only attachments list should cost.
        enclosure_urls = [
            (getattr(enc, "href", None) or getattr(enc, "url", None) or "").strip() for enc in _filtered_file_enclosures(entry, None)
        ]
        return content_html, base_url, [u for u in enclosure_urls if u]
    detail = starred_archive_service.get_archived_entry_detail(feed_url, entry_id)
    if detail:
        return detail.get("content_html") or "", str(detail.get("link") or entry_id), []
    return "", entry_id, []


@router.get("/entries/attachments")
def entry_attachments_route(feed_url: str = Query(...), entry_id: str = Query(...)):
    """Kept (already-archived) and available (found in the body, not kept)
    non-image attachments for one entry -- the article pane's Attachments
    panel. "Available" ignores the feed's attachment-extension policy on
    purpose: it is exactly the list of file links that policy doesn't cover,
    so the user can save one anyway without widening the feed's policy."""
    kept = starred_archive_service.list_non_image_assets(feed_url, entry_id)
    # The local copy, not the publisher's URL -- same rule _attachment_list_item
    # applies to the server-rendered footer (see its docstring): a saved post
    # whose files still 404 at the source has kept the wrong half.
    for item in kept:
        item["local_url"] = f"{STARRED_ASSET_URL_PREFIX}{item.pop('asset_hash')}"
    kept_urls = {k["source_url"] for k in kept}
    content_html, base_url, enclosure_urls = _entry_content_html_and_base(feed_url, entry_id)
    candidates = candidate_attachment_links_in_html(content_html, base_url)
    # Enclosure-declared files (a magazine's issue PDF/EPUB, a devblog's demo
    # video) routinely never appear as a body link at all -- see
    # _filtered_file_enclosures. Without adding these, un-keeping one left no
    # way for the panel to offer it again.
    all_candidates = list(candidates)
    seen_candidates = set(candidates)
    for url in enclosure_urls:
        if url not in seen_candidates:
            all_candidates.append(url)
            seen_candidates.add(url)
    available = [u for u in all_candidates if u not in kept_urls]
    return JSONResponse({"ok": True, "kept": kept, "available": available})


@router.post("/entries/attachments/delete")
def delete_entry_attachment_route(feed_url: str = Form(...), entry_id: str = Form(...), source_url: str = Form(...)):
    ok = starred_archive_service.delete_one_attachment(feed_url, entry_id, source_url)
    return JSONResponse({"ok": ok})


@router.post("/entries/attachments/delete-all")
def delete_all_entry_attachments_route(feed_url: str = Form(...), entry_id: str = Form(...)):
    removed = starred_archive_service.delete_all_attachments(feed_url, entry_id)
    return JSONResponse({"ok": True, "removed": removed})


@router.post("/entries/attachments/save")
def save_entry_attachment_route(feed_url: str = Form(...), entry_id: str = Form(...), source_url: str = Form(...)):
    ok = starred_archive_service.archive_one_attachment(feed_url, entry_id, source_url)
    return JSONResponse({"ok": ok, "error": None if ok else "Couldn't save that file — see server logs."})


@router.post("/entries/attachments/save-all")
def save_all_entry_attachments_route(feed_url: str = Form(...), entry_id: str = Form(...), urls: str = Form(...)):
    """Save every URL in *urls* (the panel's own currently-displayed
    "available" list, round-tripped rather than re-resolved here) as an
    attachment. Round-tripping avoids re-scanning under a second's staleness
    with the GET route while still keeping the resolution logic in one place."""
    try:
        url_list = json.loads(urls)
        assert isinstance(url_list, list)
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "Bad urls payload."}, status_code=400)
    saved = failed = 0
    for u in url_list:
        if starred_archive_service.archive_one_attachment(feed_url, entry_id, str(u)):
            saved += 1
        else:
            failed += 1
    return JSONResponse({"ok": True, "saved": saved, "failed": failed})


@router.post("/entries/move-to-feed")
def move_entry_to_feed_route(
    feed_url: str = Form(...),
    entry_id: str = Form(...),
    target_url: str = Form(...),
):
    """Move one entry's curation (star, manual tags, read state) to another feed.

    The entry is matched into the target by GUID/normalized link, or synthesized
    there; the source copy is marked read and stripped of its star/tags.
    """
    try:
        with get_reader() as reader:
            with get_meta_connection() as conn:
                result = _move_entry_to_feed(reader, conn, feed_url.strip(), entry_id, target_url.strip())
    except Exception:  # noqa: BLE001 — details stay in the log, not the response
        LOGGER.exception("[move-entry] failed for %s in %s", entry_id, feed_url)
        return JSONResponse({"ok": False, "error": "Move failed — see server logs."}, status_code=502)
    if not result["ok"]:
        return JSONResponse({"ok": False, "error": result["error"]}, status_code=400)
    bits = []
    if result["star"]:
        bits.append("star")
    if result["tags"]:
        bits.append(f"{result['tags']} tag{'s' if result['tags'] != 1 else ''}")
    carried = f" (moved {' + '.join(bits)})" if bits else ""
    return JSONResponse({"ok": True, "synthesized": result["synth"], "message": f"Entry moved{carried}."})


@router.post("/entries/move-to-feed-batch")
def move_entries_to_feed_batch_route(
    entries: str = Form(...),
    target_url: str = Form(...),
):
    """Move a batch of entries' curation to another feed.

    ``entries`` is a JSON array of ``[feed_url, entry_id]`` pairs (the visible
    post list). Entries already living in the target feed are skipped, not
    errors — a folder-wide "move visible" naturally includes the target's own
    posts. Per-entry semantics are ``_move_entry_to_feed``'s.
    """
    target = target_url.strip()
    try:
        pairs = json.loads(entries)
        assert isinstance(pairs, list)
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "Bad entries payload."}, status_code=400)
    if len(pairs) > _MOVE_BATCH_CAP:
        return JSONResponse(
            {"ok": False, "error": f"Too many entries (max {_MOVE_BATCH_CAP} per move)."},
            status_code=400,
        )
    moved = skipped = failed = stars = tags = 0
    try:
        with get_reader() as reader:
            with get_meta_connection() as conn:
                for pair in pairs:
                    if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
                        failed += 1
                        continue
                    feed_url, entry_id = str(pair[0]).strip(), str(pair[1])
                    if feed_url == target:
                        skipped += 1
                        continue
                    result = _move_entry_to_feed(reader, conn, feed_url, entry_id, target)
                    if result["ok"]:
                        moved += 1
                        stars += 1 if result["star"] else 0
                        tags += result["tags"]
                    else:
                        failed += 1
                        LOGGER.warning("[move-entry] batch item failed %s in %s: %s", entry_id, feed_url, result["error"])
    except Exception:  # noqa: BLE001 — details stay in the log, not the response
        LOGGER.exception("[move-entry] batch move to %s failed", target)
        return JSONResponse({"ok": False, "error": "Move failed — see server logs."}, status_code=502)
    bits = []
    if stars:
        bits.append(f"{stars} star{'s' if stars != 1 else ''}")
    if tags:
        bits.append(f"{tags} tag{'s' if tags != 1 else ''}")
    carried = f" (carried {' + '.join(bits)})" if bits else ""
    msg = f"Moved {moved} entr{'ies' if moved != 1 else 'y'}{carried}."
    if skipped:
        msg += f" {skipped} already in that feed."
    if failed:
        msg += f" {failed} failed — see server logs."
    return JSONResponse({"ok": True, "moved": moved, "skipped": skipped, "failed": failed, "message": msg})


# Effectively unbounded, for the same reason _RANGE_READ_LIMIT is: a whole-set
# action has to see the whole current view, not the page the browser happens to
# hold. The list route clips to 250 on first load and 2,000 thereafter, so an
# id-list move silently covered a fraction of a large filter.
_MOVE_VISIBLE_LIMIT = 1_000_000


_DURATION_FILTER_RE = re.compile(r"^(<=|>=|<|>)(.+)$")


def _parse_duration_filter_seconds(text: str) -> float | None:
    """Parse "2:00", "1:02:03", "2m", "120s", "1h", or a bare "2" (minutes) —
    mirrors the client-side parser in app.js's postsFilterMatches exactly, so
    "move the N shown" (or Select All) resolves to the same set the filter
    box is showing, same reasoning as the title/link/feed_title match below.
    """
    m = re.match(r"^(\d+):(\d{2}):(\d{2})$", text)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    m = re.match(r"^(\d+):(\d{2})$", text)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([hms])$", text)
    if m:
        n = float(m.group(1))
        unit = m.group(2)
        return n * 3600 if unit == "h" else (n * 60 if unit == "m" else n)
    m = re.match(r"^(\d+(?:\.\d+)?)$", text)
    if m:
        return float(m.group(1)) * 60
    return None


def _parse_duration_filter(term: str) -> tuple[str, float] | None:
    m = _DURATION_FILTER_RE.match(term)
    if not m:
        return None
    seconds = _parse_duration_filter_seconds(m.group(2).strip())
    if seconds is None:
        return None
    return m.group(1), seconds


def _folder_is_yt_folder(folder_id: int | None) -> bool:
    if folder_id is None:
        return False
    with get_meta_connection() as conn:
        row = conn.execute("SELECT name FROM folders WHERE id = ?", (folder_id,)).fetchone()
    return bool(row) and str(row["name"]) == get_yt_folder_name()


def _view_filter_predicate(term: str | None, *, folder_id: int | None = None):
    """The posts list's "Filter this view" box, resolved server-side.

    Matches the same three fields the browser-side filter matches — post title,
    link URL, and source feed title — so "move the N shown" resolves to exactly
    the set the filter is showing. The client filters locally for instant feel;
    this is what the move itself is settled against.

    Deliberately narrower than ``search_query``, which also spans summary and
    authors: a filter narrows what is already in front of you, a search changes
    what is fetched.

    Duration syntax ("<2:00" etc.) is recognized only when *folder_id* is the
    configured YouTube folder — same gate as the client, so a title that
    happens to look like duration syntax elsewhere still text-matches.
    """
    needle = (term or "").strip().lower()
    if not needle:
        return lambda post: True

    if _folder_is_yt_folder(folder_id):
        duration = _parse_duration_filter(needle)
        if duration is not None:
            op, seconds = duration

            def _matches_duration(post: dict) -> bool:
                dur = post.get("duration_seconds")
                if dur is None:
                    return False
                if op == "<":
                    return dur < seconds
                if op == "<=":
                    return dur <= seconds
                if op == ">":
                    return dur > seconds
                if op == ">=":
                    return dur >= seconds
                return False

            return _matches_duration

    def _matches(post: dict) -> bool:
        return any(needle in str(post.get(key) or "").lower() for key in ("title", "link", "feed_title"))

    return _matches


def _resolve_view_posts(
    *,
    folder_id: int,
    list_feed_url: str | None,
    tag: str | None,
    sort_by: str | None,
    sort_dir: str | None,
    read_filter: str | None,
    star_only: str | None,
    search_query: str | None,
    inbox_view: bool,
) -> list[dict]:
    """Re-resolve the post list a browser view is showing, unclipped.

    Mirrors the home route's scope derivation (disabled-feed exclusion, the
    kept-feed and Saved Articles widening under the star filter, and the
    search-forces-``all`` rule) so a whole-set action lands on the same entries
    the user is looking at.

    Orphan archive entries ARE merged in, same condition and same
    ``merge_orphan_saved_entries`` call the home route's whole-backlog star view
    uses — they used to be skipped here ("no reader row to act on"), but
    ``_move_entry_to_feed`` now has an orphan-archive fallback (see its
    docstring), so leaving them out just meant Select All and "Move visible to
    feed…" silently dropped exactly the rows a user was trying to consolidate.
    Reported live 2026-09-14: searching Saved for a stuck feed's own domain
    displayed its orphaned stars fine (the home route already merges them) but
    Select All picked only the *other*, reader-backed search hits — every
    genuine match was silently missing from the selection.
    """
    normalized_tag = normalize_tag_value(tag)
    normalized_query = normalize_search_query(search_query)
    normalized_star_only = normalize_star_only(star_only)
    normalized_read_filter = normalize_read_filter(read_filter)
    # Search always spans All, exactly as the home route widens it — otherwise
    # the set resolved here would be narrower than the list on screen.
    if normalized_query and normalized_read_filter in {"all", "unread"}:
        normalized_read_filter = "all"

    with get_meta_connection() as conn:
        feed_urls = get_folder_feed_urls(conn, folder_id)
        disabled_feed_urls = get_disabled_feed_urls(conn)
        root_id = get_root_folder_id(conn)

    entry_feed_urls = filter_feed_urls(feed_urls, list_feed_url)
    # Stars are deliberate curation and stay visible on a disabled feed, so the
    # star view must not subtract them here either.
    if not (list_feed_url or normalized_star_only):
        entry_feed_urls = entry_feed_urls - disabled_feed_urls
    if normalized_star_only and folder_id == root_id and not list_feed_url:
        # Same folder-scoping gate as the home route (main.py's _home_inner) --
        # a kept-but-unsubscribed feed has no folder_feeds row, so it belongs
        # to the whole-library view, not every folder. See that gate's comment
        # for the live incident this was missing here too.
        entry_feed_urls = entry_feed_urls | get_kept_feed_urls()
        entry_feed_urls = entry_feed_urls | {saved_articles_service.SAVED_FEED_URL}

    posts = list_entries_for_feeds(
        entry_feed_urls,
        limit=_MOVE_VISIBLE_LIMIT,
        sort_by=normalize_sort_by(sort_by, allow_starred=True),
        sort_dir=normalize_sort_dir(sort_dir),
        read_filter=normalized_read_filter,
        star_only=normalized_star_only,
        selected_tag=normalized_tag,
        search_query=normalized_query,
        kept_scope=("starred" if inbox_view else "kept"),
    )

    # Same gate the home route uses for its own whole-backlog star view: root,
    # no single feed or tag narrowing it — a scope orphans (which have no
    # folder of their own) can meaningfully belong to.
    if normalized_star_only and not list_feed_url and not normalized_tag and folder_id == root_id:
        try:
            posts = merge_orphan_saved_entries(
                posts,
                live_feed_urls=get_all_reader_feed_urls(),
                sort_by=normalize_sort_by(sort_by, allow_starred=True),
                sort_dir=normalize_sort_dir(sort_dir),
                limit=_MOVE_VISIBLE_LIMIT,
                search_terms=search_terms_from_query(normalized_query),
                kept_scope=("starred" if inbox_view else "kept"),
            )
        except Exception:  # noqa: BLE001 — the live-entry resolution above still stands
            LOGGER.exception("[resolve-view] orphan merge failed")
    return posts


@router.post("/entries/select-all-visible")
def select_all_visible_entries_route(
    folder_id: int = Form(...),
    list_feed_url: str | None = Form(default=None),
    tag: str | None = Form(default=None),
    sort_by: str | None = Form(default=None),
    sort_dir: str | None = Form(default=None),
    read_filter: str | None = Form(default=None),
    star_only: str | None = Form(default=None),
    q: str | None = Form(default=None),
    kept: str | None = Form(default=None),
    filter_term: str = Form(default=""),
):
    """Resolve every entry matching the current view + filter, for the post
    list's Select All button.

    Same whole-view resolution move-visible-to-feed uses (_resolve_view_posts
    + _view_filter_predicate) — unclipped and independent of scroll-chunking
    — but returns the entries themselves rather than moving or counting them,
    so the client can populate the multi-select for rows not yet rendered."""
    try:
        posts = _resolve_view_posts(
            folder_id=folder_id,
            list_feed_url=list_feed_url,
            tag=tag,
            sort_by=sort_by,
            sort_dir=sort_dir,
            read_filter=read_filter,
            star_only=star_only,
            search_query=q,
            inbox_view=(kept or "").strip().lower() == "starred",
        )
    except Exception:  # noqa: BLE001 — details stay in the log, not the response
        LOGGER.exception("[select-all] could not resolve the view")
        return JSONResponse({"ok": False, "error": "Could not read the current view."}, status_code=502)

    matches_filter = _view_filter_predicate(filter_term, folder_id=folder_id)
    entries = [
        {"feedUrl": str(p["feed_url"]), "entryId": str(p["id"]), "videoId": str(p.get("video_id") or "")}
        for p in posts
        if matches_filter(p)
    ]
    return JSONResponse({"ok": True, "entries": entries, "count": len(entries)})


@router.post("/entries/move-visible-to-feed")
def move_visible_entries_to_feed_route(
    target_url: str = Form(default=""),
    folder_id: int = Form(...),
    list_feed_url: str | None = Form(default=None),
    tag: str | None = Form(default=None),
    sort_by: str | None = Form(default=None),
    sort_dir: str | None = Form(default=None),
    read_filter: str | None = Form(default=None),
    star_only: str | None = Form(default=None),
    q: str | None = Form(default=None),
    kept: str | None = Form(default=None),
    filter_term: str = Form(default=""),
    dry_run: str = Form(default=""),
):
    """Move every entry matching the current view + filter, resolved server-side.

    The id-list sibling (``/entries/move-to-feed-batch``) can only move what the
    browser holds, which is a page of the view rather than the view — so a
    filter matching more than the loaded chunk moved a silent fraction of it.
    This route takes the *predicate* instead (scope, filters, filter term) and
    re-resolves it here, which is correct at any size. That also means there is
    no id payload to bound, so ``_MOVE_BATCH_CAP`` does not apply.

    ``dry_run`` returns just the count, so the confirm dialog can state the real
    number instead of the number of rows in the DOM.
    """
    target = target_url.strip()
    is_dry_run = dry_run in ("1", "true", "on", "yes")
    if not is_dry_run and not target:
        return JSONResponse({"ok": False, "error": "Pick a target feed."}, status_code=400)

    try:
        posts = _resolve_view_posts(
            folder_id=folder_id,
            list_feed_url=list_feed_url,
            tag=tag,
            sort_by=sort_by,
            sort_dir=sort_dir,
            read_filter=read_filter,
            star_only=star_only,
            search_query=q,
            inbox_view=(kept or "").strip().lower() == "starred",
        )
    except Exception:  # noqa: BLE001 — details stay in the log, not the response
        LOGGER.exception("[move-entry] could not resolve the view for a move to %s", target)
        return JSONResponse({"ok": False, "error": "Could not read the current view."}, status_code=502)

    matches_filter = _view_filter_predicate(filter_term, folder_id=folder_id)
    matches = [p for p in posts if matches_filter(p)]

    if is_dry_run:
        return JSONResponse({"ok": True, "count": len(matches)})

    moved = skipped = failed = stars = tags = 0
    try:
        with get_reader() as reader:
            with get_meta_connection() as conn:
                for post in matches:
                    feed_url, entry_id = str(post["feed_url"]), str(post["id"])
                    if feed_url == target:
                        skipped += 1
                        continue
                    result = _move_entry_to_feed(reader, conn, feed_url, entry_id, target)
                    if result["ok"]:
                        moved += 1
                        stars += 1 if result["star"] else 0
                        tags += result["tags"]
                    else:
                        failed += 1
                        LOGGER.warning("[move-entry] visible-move item failed %s in %s: %s", entry_id, feed_url, result["error"])
    except Exception:  # noqa: BLE001 — details stay in the log, not the response
        LOGGER.exception("[move-entry] visible move to %s failed", target)
        return JSONResponse({"ok": False, "error": "Move failed — see server logs."}, status_code=502)

    bits = []
    if stars:
        bits.append(f"{stars} star{'s' if stars != 1 else ''}")
    if tags:
        bits.append(f"{tags} tag{'s' if tags != 1 else ''}")
    carried = f" (carried {' + '.join(bits)})" if bits else ""
    msg = f"Moved {moved} entr{'ies' if moved != 1 else 'y'}{carried}."
    if skipped:
        msg += f" {skipped} already in that feed."
    if failed:
        msg += f" {failed} failed — see server logs."
    return JSONResponse({"ok": True, "moved": moved, "skipped": skipped, "failed": failed, "message": msg})


@router.post("/entries/purge")
async def purge_old_entries(request: Request):
    """Purge utility (Settings → Feeds → Utilities): delete posts older than a
    date from the selected folders.

    Body (JSON): {"folder_ids": [..], "before": "YYYY-MM-DD",
                  "include_unread": bool, "dry_run": bool}
    dry_run returns the matching count without deleting. Protections are
    _prune_entries': starred/saved posts, manually tagged posts, and the
    Saved Articles feed are never touched."""
    body = await request.json()
    try:
        folder_ids = [int(f) for f in body.get("folder_ids", [])]
    except TypeError, ValueError:
        return JSONResponse({"ok": False, "error": "Bad folder_ids."}, status_code=400)
    try:
        cutoff = datetime.fromisoformat(str(body.get("before", "")))
    except ValueError:
        return JSONResponse({"ok": False, "error": "Bad 'before' date."}, status_code=400)
    if not folder_ids:
        return JSONResponse({"ok": False, "error": "No folders selected."}, status_code=400)
    include_unread = bool(body.get("include_unread", False))
    dry_run = bool(body.get("dry_run", False))

    feed_urls: set[str] = set()
    with get_meta_connection() as conn:
        for fid in folder_ids:
            feed_urls |= get_folder_feed_urls(conn, fid)
    count = _prune_entries(
        sorted(feed_urls),
        published_cutoff=cutoff,
        include_unread=include_unread,
        dry_run=dry_run,
    )
    if not dry_run and count:
        LOGGER.info("[purge] deleted %d posts older than %s from folders %s (unread=%s)", count, cutoff.date(), folder_ids, include_unread)
    return JSONResponse({"ok": True, "count": count, "dry_run": dry_run})


@router.get("/entries/manual-tags-batch")
def get_entries_manual_tags_batch_route(entries: str = Query(...)):
    """Tag coverage across a selection (one entry or many) — populates the
    Edit Tags dialog's chip picker, added 2026-08-31 so a bulk edit shows
    what's actually there instead of a blank box the user has to guess a
    tag's exact stored spelling into (multi-word tags collapse whitespace to
    hyphens at normalize time, so "science + math" is stored as
    "science-+-math" — a chip to click is far more reliable than retyping
    that by hand). ``counts[tag]`` is how many of the selected entries carry
    it; a tag present on every entry (``counts[tag] == total``) renders as a
    normal chip client-side, a partial one dimmed."""
    try:
        pairs = json.loads(entries)
        assert isinstance(pairs, list)
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "Bad entries payload."}, status_code=400)
    if len(pairs) > _MOVE_BATCH_CAP:
        return JSONResponse(
            {"ok": False, "error": f"Too many entries (max {_MOVE_BATCH_CAP} per action)."},
            status_code=400,
        )
    counts: dict[str, int] = {}
    total = 0
    for pair in pairs:
        if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
            continue
        feed_url, entry_id = str(pair[0]).strip(), str(pair[1])
        total += 1
        for tag in get_manual_tags_for_entry(feed_url, entry_id):
            counts[tag] = counts.get(tag, 0) + 1
    return JSONResponse({"ok": True, "counts": counts, "total": total})


def _merge_manual_tags(existing_tags: list[str], added_raw_tags: str) -> str:
    """Append newly-typed tags onto an entry's existing manual tags.

    Shared by the single-entry append path and the bulk tag-add route — both
    need the same normalize/dedupe/cap-at-``MAX_MANUAL_TAGS`` rule.
    """
    appended_tags = parse_manual_hashtags(added_raw_tags)
    merged_tags: list[str] = []
    seen: set[str] = set()
    for _tok in existing_tags + appended_tags:
        normalized = normalize_tag_value(_tok)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        merged_tags.append(normalized)
        if len(merged_tags) >= MAX_MANUAL_TAGS:
            break
    return " ".join(merged_tags)


@router.post("/entries/tags")
def set_entry_manual_tags(
    request: Request,
    folder_id: int = Form(...),
    feed_url: str = Form(...),
    entry_id: str = Form(...),
    tags_text: str = Form(default=""),
    list_feed_url: str | None = Form(default=None),
    tag: str | None = Form(default=None),
    sort_by: str | None = Form(default=None),
    sort_dir: str | None = Form(default=None),
    read_filter: str | None = Form(default=None),
    star_only: str | None = Form(default=None),
    resume_read_filter: str | None = Form(default=None),
    select_entry: int = Form(default=1),
    append_mode: int = Form(default=0),
):
    if append_mode:
        existing_tags = get_manual_tags_for_entry(feed_url, entry_id)
        tags = set_manual_tags_for_entry(feed_url, entry_id, _merge_manual_tags(existing_tags, tags_text))
    else:
        tags = set_manual_tags_for_entry(feed_url, entry_id, tags_text)
    normalized_tag = normalize_tag_value(tag)  # `tag` = the active tag filter, not the loop var

    # Tagging is a keep signal, so a stub gets the same treatment as a star.
    # Only when tags remain: clearing the last tag is the opposite of keeping.
    # Deliberately here and not in set_manual_tags_for_entry, which the feed
    # auto-taggers also drive across a whole refresh — see _maybe_autofetch_on_keep.
    autofetch_pending = False
    if tags:
        autofetch_pending = _maybe_autofetch_on_keep(feed_url, entry_id)

    list_feed_query = f"&list_feed_url={quote_plus(list_feed_url)}" if list_feed_url else ""
    tag_query = f"&tag={quote_plus(normalized_tag)}" if normalized_tag else ""
    _nrf_et = normalize_read_filter(read_filter)
    sort_query = build_sort_query(sort_by, sort_dir)
    read_filter_query = build_read_filter_query(read_filter)
    star_only_query = build_star_only_query(star_only)
    resume_read_filter_query = build_resume_read_filter_query(resume_read_filter, active_read_filter=_nrf_et)

    entry_query = _entry_query_suffix(feed_url, entry_id, include=bool(select_entry))
    message = "Tags updated." if tags else "Tags cleared."

    if request.headers.get("X-Requested-With") == "lectio-ajax":
        return JSONResponse({"ok": True, "tags": tags, "autofetch_pending": autofetch_pending})

    return RedirectResponse(
        url=(
            f"/?folder_id={folder_id}{list_feed_query}{tag_query}{sort_query}{read_filter_query}{star_only_query}{resume_read_filter_query}{entry_query}"
            f"&message={quote_plus(message)}"
        ),
        status_code=303,
    )


@router.post("/entries/tags-batch")
def edit_manual_tags_on_entries_batch_route(
    entries: str = Form(...),
    tags_text: str = Form(...),
):
    """Add and/or remove tags across a batch of entries' manual tags.

    ``entries`` is a JSON array of ``[feed_url, entry_id]`` pairs (the post
    list's multi-selection) — same shape as ``/entries/move-to-feed-batch``.
    ``tags_text`` uses the same ``+/-tag`` convention as the rule editor's
    tag_filter spec (``-tag`` removes, bare/``+tag`` adds) — see
    ``parse_manual_tag_edit_tokens``. Each entry's OWN existing tags are the
    base for the edit (unlike the single-entry route's replace mode, which
    would blank out tags a different selected post already had), so removing
    a tag one post doesn't have is simply a no-op for that post.

    Renamed from "Add tag" to "Edit tags" 2026-08-31 (Josh: a bulk add with no
    way to also remove was a footgun — meant editing 3 posts' tags but had to
    fall back to doing each by hand). Returns per-entry ``still_tagged``/
    ``now_untagged`` pairs (not a blanket "every touched entry now has a tag"
    the way the append-only route could assume) so the client updates each
    post's tag/kept indicator correctly rather than assuming success implies
    "has tags."
    """
    try:
        pairs = json.loads(entries)
        assert isinstance(pairs, list)
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "Bad entries payload."}, status_code=400)
    if len(pairs) > _MOVE_BATCH_CAP:
        return JSONResponse(
            {"ok": False, "error": f"Too many entries (max {_MOVE_BATCH_CAP} per action)."},
            status_code=400,
        )
    add_tokens, remove_set = parse_manual_tag_edit_tokens(tags_text)
    if not add_tokens and not remove_set:
        return JSONResponse({"ok": False, "error": "No valid tags."}, status_code=400)

    updated = failed = 0
    still_tagged: list[list[str]] = []
    now_untagged: list[list[str]] = []
    for pair in pairs:
        if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
            failed += 1
            continue
        feed_url, entry_id = str(pair[0]).strip(), str(pair[1])
        try:
            existing_tags = get_manual_tags_for_entry(feed_url, entry_id)
            new_tags = apply_manual_tag_edits(existing_tags, add_tokens, remove_set)
            set_manual_tags_for_entry(feed_url, entry_id, " ".join(new_tags))
            updated += 1
            (still_tagged if new_tags else now_untagged).append([feed_url, entry_id])
        except Exception as exc:  # noqa: BLE001 — one bad entry must not sink the batch
            failed += 1
            LOGGER.warning("[tags-batch] failed to edit tags on %s in %s: %s", entry_id, feed_url, exc)

    msg = f"Updated tags on {updated} post{'s' if updated != 1 else ''}."
    if failed:
        msg += f" {failed} failed."
    return JSONResponse(
        {
            "ok": True,
            "tagged": updated,
            "failed": failed,
            "message": msg,
            "still_tagged": still_tagged,
            "now_untagged": now_untagged,
        }
    )


@router.post("/entries/discard")
def discard_entry(
    request: Request,
    feed_url: str = Form(...),
    entry_id: str = Form(...),
):
    """Delete — the other half of the read-later triage pair.

    Josh's definition (2026-07-29): *"I'm done with this but don't necessarily
    need its contents stored; don't necessarily delete it now, but also don't
    protect it anymore."* So: drop every keep signal (star **and** tags), mark it
    read at both levels, and let the offline capture and pruning-exemption go
    with them. The entry itself is not deleted — it goes back to being an
    ordinary feed post and takes its chances with per-folder retention. The one
    exception is a `lectio:saved` husk, which nothing else holds and no view
    would ever show; `apply_star_state` removes those outright.

    This exists as a route because the gesture has an **order** the client was
    previously trusted to get right: **every** keep signal must be cleared before
    the unstar, since the capture is only released once none remains. Reversed,
    the captured copy is stranded with nothing keeping it. That is server-side
    knowledge, so it now lives on the server.

    That includes the **archived** row, which is easy to forget and fails
    quietly twice over: the entry stays listed in Archive forever, and — because
    archived *is* a keep signal — the unstar below sees one and skips releasing
    the offline capture, so Delete keeps the contents it exists to drop.
    Deleting an archived item is not a contradiction: Archive means "done, keep
    it", Delete means "done, don't", so Delete has to win.
    """
    set_manual_tags_for_entry(feed_url, entry_id, "")
    set_entry_archived(feed_url, entry_id, False)
    apply_star_state(feed_url, entry_id, False)
    mark_entry_read_everywhere(feed_url, entry_id)
    return JSONResponse({"ok": True, "feed_url": feed_url, "entry_id": entry_id, "discarded": True})


@router.post("/entries/read")
def mark_entry_read(
    request: Request,
    folder_id: int = Form(...),
    feed_url: str = Form(...),
    entry_id: str = Form(...),
    read: int = Form(...),
    list_feed_url: str | None = Form(default=None),
    tag: str | None = Form(default=None),
    sort_by: str | None = Form(default=None),
    sort_dir: str | None = Form(default=None),
    read_filter: str | None = Form(default=None),
    star_only: str | None = Form(default=None),
    resume_read_filter: str | None = Form(default=None),
    select_entry: int = Form(default=1),
):
    normalized_tag = normalize_tag_value(tag)
    is_async = is_async_action_request(request, "lectio-post-read-toggle") or is_async_action_request(request, "lectio-entry-read-toggle")
    include_history = is_async_action_request(request, "lectio-entry-read-toggle")

    # For async toggles (post-list checkbox, entry pane toggle) pre-read the
    # entry/feed for history while the reader lock is not contested, then fire
    # the writes in a background thread so the JSON response returns immediately
    # regardless of any ongoing background-refresh write lock on the reader DB.
    if is_async:
        _title, _link, _feed_title = "", "", ""
        if read and include_history:
            with get_reader() as reader:
                entry_obj = reader.get_entry((feed_url, entry_id), None)
                feed_obj = reader.get_feed(feed_url, None)
            _title = str(getattr(entry_obj, "title", None) or "")
            _link = str(getattr(entry_obj, "link", None) or "")
            _feed_title = str(getattr(feed_obj, "title", None) or "")

        _fu, _eid, _read, _do_hist = feed_url, entry_id, bool(read), include_history
        _hist_args = (_title, _link, _feed_title)
        # Capture the current user: the daemon thread below does not inherit this
        # request's contextvars, so without re-binding the (un)read write would
        # land in the default (legacy) user's DB instead of the actual user's.
        _uid = tenancy.current_user_id()

        def _bg_toggle() -> None:
            try:
                with get_reader() as reader:
                    if _read:
                        reader.mark_entry_as_read((_fu, _eid))
                    else:
                        reader.mark_entry_as_unread((_fu, _eid))
            except Exception:
                LOGGER.warning("background mark_entry_(un)read failed for %s/%s", _fu, _eid, exc_info=True)
            try:
                if _read:
                    upsert_entry_read_state(_fu, _eid)
                else:
                    delete_entry_read_state(_fu, _eid)
            except Exception:
                LOGGER.warning("background entry_read_state write failed for %s/%s", _fu, _eid, exc_info=True)
            if _read and _do_hist:
                try:
                    append_read_history(_fu, _eid, *_hist_args)
                except Exception:
                    LOGGER.warning("background append_read_history failed for %s/%s", _fu, _eid, exc_info=True)
            with unread_counts_cache_lock:
                _bump_unread_counts_generation()
                unread_counts_cache.clear()

        threading.Thread(target=_run_in_user_context, args=(_uid, _bg_toggle), daemon=True).start()
        return JSONResponse({"ok": True, "feed_url": feed_url, "entry_id": entry_id, "read": bool(read)})

    # Synchronous (full-page redirect) path — wait for writes before redirecting
    # so the reloaded page reflects the correct read state.
    with get_reader() as reader:
        if read:
            reader.mark_entry_as_read((feed_url, entry_id))
            try:
                upsert_entry_read_state(feed_url, entry_id)
            except Exception:
                LOGGER.warning("upsert_entry_read_state failed in mark_entry_read (db contention?)", exc_info=True)
        else:
            reader.mark_entry_as_unread((feed_url, entry_id))
            try:
                delete_entry_read_state(feed_url, entry_id)
            except Exception:
                LOGGER.warning("delete_entry_read_state failed in mark_entry_read (db contention?)", exc_info=True)
    with unread_counts_cache_lock:
        _bump_unread_counts_generation()
        unread_counts_cache.clear()

    list_feed_query = f"&list_feed_url={quote_plus(list_feed_url)}" if list_feed_url else ""
    tag_query = f"&tag={quote_plus(normalized_tag)}" if normalized_tag else ""
    _nrf_er = normalize_read_filter(read_filter)
    sort_query = build_sort_query(sort_by, sort_dir)
    read_filter_query = build_read_filter_query(read_filter)
    star_only_query = build_star_only_query(star_only)
    resume_read_filter_query = build_resume_read_filter_query(resume_read_filter, active_read_filter=_nrf_er)

    entry_query = _entry_query_suffix(feed_url, entry_id, include=bool(select_entry))

    return RedirectResponse(
        url=f"/?folder_id={folder_id}{list_feed_query}{tag_query}{sort_query}{read_filter_query}{star_only_query}{resume_read_filter_query}{entry_query}",
        status_code=303,
    )


@router.post("/entries/saved")
def toggle_entry_saved(
    request: Request,
    folder_id: int = Form(...),
    feed_url: str = Form(...),
    entry_id: str = Form(...),
    saved: int = Form(...),
    list_feed_url: str | None = Form(default=None),
    tag: str | None = Form(default=None),
    sort_by: str | None = Form(default=None),
    sort_dir: str | None = Form(default=None),
    read_filter: str | None = Form(default=None),
    star_only: str | None = Form(default=None),
    resume_read_filter: str | None = Form(default=None),
    select_entry: int = Form(default=1),
):
    normalized_tag = normalize_tag_value(tag)

    undo_token: str | None = None
    if not saved:
        # Capture the pre-delete saved_at (not just "it was starred") so a
        # short-lived undo restores the entry to its actual star-order
        # position instead of jumping to "just starred, at the top."
        with get_meta_connection() as conn:
            _prior = conn.execute(
                "SELECT saved_at FROM saved_entries WHERE feed_url = ? AND entry_id = ?",
                (feed_url, entry_id),
            ).fetchone()
        if _prior is not None:
            undo_token = datetime.now().isoformat()
            with get_meta_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO entry_unstar_batch (feed_url, entry_id, unstarred_at, saved_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(feed_url, entry_id) DO UPDATE SET
                        unstarred_at = excluded.unstarred_at,
                        saved_at = excluded.saved_at
                    """,
                    (feed_url, entry_id, undo_token, _prior["saved_at"]),
                )
                conn.commit()

    apply_star_state(feed_url, entry_id, bool(saved))
    autofetch_pending = False
    if saved:
        autofetch_pending = _maybe_autofetch_on_keep(feed_url, entry_id)

    if is_async_action_request(request, "lectio-post-save-toggle"):
        return JSONResponse(
            {
                "ok": True,
                "feed_url": feed_url,
                "entry_id": entry_id,
                "saved": bool(saved),
                "undo_token": undo_token,
                "autofetch_pending": autofetch_pending,
            }
        )

    if is_async_action_request(request, "lectio-entry-save-toggle"):
        return JSONResponse(
            {
                "ok": True,
                "feed_url": feed_url,
                "entry_id": entry_id,
                "saved": bool(saved),
                "undo_token": undo_token,
                "autofetch_pending": autofetch_pending,
            }
        )

    list_feed_query = f"&list_feed_url={quote_plus(list_feed_url)}" if list_feed_url else ""
    tag_query = f"&tag={quote_plus(normalized_tag)}" if normalized_tag else ""
    _nrf_es = normalize_read_filter(read_filter)
    sort_query = build_sort_query(sort_by, sort_dir)
    read_filter_query = build_read_filter_query(read_filter)
    star_only_query = build_star_only_query(star_only)
    resume_read_filter_query = build_resume_read_filter_query(resume_read_filter, active_read_filter=_nrf_es)

    entry_query = _entry_query_suffix(feed_url, entry_id, include=bool(select_entry))

    return RedirectResponse(
        url=f"/?folder_id={folder_id}{list_feed_query}{tag_query}{sort_query}{read_filter_query}{star_only_query}{resume_read_filter_query}{entry_query}",
        status_code=303,
    )


@router.post("/entries/archive")
def toggle_entry_archived(
    request: Request,
    feed_url: str = Form(...),
    entry_id: str = Form(...),
    archived: int = Form(...),
):
    """Archive / un-Archive an item — the read-later **done** axis.

    Josh's definition (2026-07-29): *"Archive is essentially just mark this To
    Read item as Read. Keep its contents."* A star is a TODO, so archiving
    discharges it: the star comes off, the item leaves the inbox, and it is
    marked read at both levels because acting on something from the list is
    dealing with it.

    **Order is load-bearing.** The archived row is written *before* the unstar,
    because ``apply_star_state`` releases the offline capture (and hard-deletes a
    Saved Articles husk) when no keep signal remains. Archive is itself the keep
    signal here, so unstarring first would destroy the contents this promises to
    keep — for a URL-saved article the capture is often the only copy.

    Un-archiving is the inverse: it restores the star, putting the item back on
    the TODO pile it came from. Read state is deliberately *not* reverted — "read
    but not archived" is a real state (you read it, you still haven't decided
    what to do with it), and it is the whole reason this second axis exists.
    """
    if archived:
        set_entry_archived(feed_url, entry_id, True)
        apply_star_state(feed_url, entry_id, False)
        mark_entry_read_everywhere(feed_url, entry_id)
    else:
        apply_star_state(feed_url, entry_id, True)
        set_entry_archived(feed_url, entry_id, False)
    return JSONResponse({"ok": True, "feed_url": feed_url, "entry_id": entry_id, "archived": bool(archived)})


@router.post("/entries/read-batch")
def mark_entries_read_batch_route(entries: str = Form(...), read: int = Form(default=1)):
    """Mark a batch of entries read or unread — the post list's multi-selection
    bulk action.

    ``entries`` is a JSON array of ``[feed_url, entry_id]`` pairs, same shape as
    ``/entries/move-to-feed-batch`` and ``/entries/tags-batch``. ``read``
    defaults to 1 (mark read, the original one-directional behavior) — pass 0
    for the "Mark as unread" bulk sibling. Both directions are explicit,
    always-visible buttons in the bulk menu (like the star/unstar pair),
    not a mixed-selection toggle: there is no single "obvious direction" to
    collapse to when the selection spans both states.
    """
    try:
        pairs = json.loads(entries)
        assert isinstance(pairs, list)
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "Bad entries payload."}, status_code=400)
    if len(pairs) > _MOVE_BATCH_CAP:
        return JSONResponse(
            {"ok": False, "error": f"Too many entries (max {_MOVE_BATCH_CAP} per action)."},
            status_code=400,
        )
    mark_read = bool(read)

    marked = failed = 0
    to_sync: list[tuple[str, str]] = []
    with get_reader() as reader:
        for pair in pairs:
            if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
                failed += 1
                continue
            feed_url, entry_id = str(pair[0]).strip(), str(pair[1])
            try:
                entry = reader.get_entry((feed_url, entry_id), None)
                if entry is not None and bool(entry.read) == mark_read:
                    continue  # already in the requested state — leave read_history/read_state alone
                if mark_read:
                    # A premiere that hasn't aired yet shouldn't be swallowed by a
                    # blanket bulk mark-read, same guard as "Read above/below".
                    # Marking unread carries no equivalent risk, so this only
                    # gates the read direction.
                    if _youtube_unpremiered_video_id(feed_url, getattr(entry, "link", None)) is not None:
                        continue
                    reader.mark_entry_as_read((feed_url, entry_id))
                else:
                    reader.mark_entry_as_unread((feed_url, entry_id))
                to_sync.append((feed_url, entry_id))
                marked += 1
            except Exception as exc:  # noqa: BLE001 — one bad entry must not sink the batch
                failed += 1
                LOGGER.warning("[read-batch] failed to mark %s %s in %s: %s", "read" if mark_read else "unread", entry_id, feed_url, exc)

    if to_sync:
        with get_meta_connection() as conn:
            if mark_read:
                when = datetime.now().isoformat()
                conn.executemany(
                    """
                    INSERT INTO entry_read_state (feed_url, entry_id, read_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(feed_url, entry_id) DO UPDATE SET read_at = excluded.read_at
                    """,
                    [(fu, eid, when) for fu, eid in to_sync],
                )
            else:
                conn.executemany(
                    "DELETE FROM entry_read_state WHERE feed_url = ? AND entry_id = ?",
                    to_sync,
                )
        invalidate_unread_counts_cache()

    verb = "read" if mark_read else "unread"
    msg = f"Marked {marked} post{'s' if marked != 1 else ''} as {verb}."
    if failed:
        msg += f" {failed} failed."
    return JSONResponse({"ok": True, "marked": marked, "failed": failed, "message": msg})


@router.post("/entries/star-batch")
def star_entries_batch_route(entries: str = Form(...), saved: int = Form(...)):
    """Add or remove the star on a batch of entries — the post list's
    multi-selection bulk action. ``entries`` is a JSON array of
    ``[feed_url, entry_id]`` pairs, same shape as ``/entries/read-batch``.

    Unlike bulk mark-as-read (always one-directional), both add and remove
    are common triage actions for stars — this route serves "Add star to N
    posts" and "Remove star from N posts" behind one `saved` flag rather than
    two near-duplicate handlers.

    Removing collects an undo batch under one shared token
    (``entry_unstar_batch``, the same mechanism the single-post unstar toggle
    already uses) — unstarring an untagged Saved Article hard-deletes it
    (``apply_star_state`` -> ``_hard_delete_entry``), so an accidental bulk
    unstar needs the same rescue the single-post one already has.
    """
    try:
        pairs = json.loads(entries)
        assert isinstance(pairs, list)
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "Bad entries payload."}, status_code=400)
    if len(pairs) > _MOVE_BATCH_CAP:
        return JSONResponse(
            {"ok": False, "error": f"Too many entries (max {_MOVE_BATCH_CAP} per action)."},
            status_code=400,
        )

    want_saved = bool(saved)
    undo_token = datetime.now().isoformat() if not want_saved else None
    changed = failed = 0

    for pair in pairs:
        if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
            failed += 1
            continue
        feed_url, entry_id = str(pair[0]).strip(), str(pair[1])
        try:
            with get_meta_connection() as conn:
                prior = conn.execute(
                    "SELECT saved_at FROM saved_entries WHERE feed_url = ? AND entry_id = ?",
                    (feed_url, entry_id),
                ).fetchone()
            already = prior is not None
            if already == want_saved:
                continue  # already in the desired state — nothing to do

            if not want_saved:
                # Same shape as the single-post toggle's undo capture: record
                # the real saved_at (not just "it was starred") so undo
                # restores the actual star-order position.
                with get_meta_connection() as conn:
                    conn.execute(
                        """
                        INSERT INTO entry_unstar_batch (feed_url, entry_id, unstarred_at, saved_at)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(feed_url, entry_id) DO UPDATE SET
                            unstarred_at = excluded.unstarred_at,
                            saved_at = excluded.saved_at
                        """,
                        (feed_url, entry_id, undo_token, prior["saved_at"]),
                    )
                    conn.commit()

            apply_star_state(feed_url, entry_id, want_saved)
            if want_saved:
                _maybe_autofetch_on_keep(feed_url, entry_id)
            changed += 1
        except Exception as exc:  # noqa: BLE001 — one bad entry must not sink the batch
            failed += 1
            LOGGER.warning("[star-batch] failed to set saved=%s for %s in %s: %s", want_saved, entry_id, feed_url, exc)

    verb = "Starred" if want_saved else "Unstarred"
    msg = f"{verb} {changed} post{'s' if changed != 1 else ''}."
    if failed:
        msg += f" {failed} failed."
    resp = {"ok": True, "changed": changed, "failed": failed, "message": msg}
    if undo_token and changed:
        resp["undo_token"] = undo_token
    return JSONResponse(resp)


@router.post("/entries/mark-range-read")
def mark_entries_range_read(
    request: Request,
    folder_id: int = Form(...),
    feed_url: str = Form(...),
    entry_id: str = Form(...),
    direction: str = Form(...),
    list_feed_url: str | None = Form(default=None),
    tag: str | None = Form(default=None),
    sort_by: str | None = Form(default=None),
    sort_dir: str | None = Form(default=None),
    read_filter: str | None = Form(default=None),
    star_only: str | None = Form(default=None),
    resume_read_filter: str | None = Form(default=None),
    q: str | None = Form(default=None),
):
    normalized_sort_by = normalize_sort_by(sort_by)
    normalized_sort_dir = normalize_sort_dir(sort_dir)
    normalized_read_filter = normalize_read_filter(read_filter)
    normalized_star_only = normalize_star_only(star_only)
    normalized_tag = normalize_tag_value(tag)
    normalized_query = normalize_search_query(q)
    # Search always spans All, exactly as the home route (and
    # _resolve_view_posts) widen it — otherwise the anchor is resolved
    # against a narrower set than the search results actually on screen.
    if normalized_query and normalized_read_filter in {"all", "unread"}:
        normalized_read_filter = "all"

    with get_meta_connection() as conn:
        feed_urls = get_folder_feed_urls(conn, folder_id)

    filtered_feed_urls = filter_feed_urls(feed_urls, list_feed_url)
    # "Above/below" needs the anchor's position in the *whole* current view, so
    # the list must not be clipped to the default page (250) — a post past that
    # cutoff read as "not in the current view". _RANGE_READ_LIMIT is effectively
    # unbounded; the folder's own size is the real bound.
    posts = list_entries_for_feeds(
        filtered_feed_urls,
        limit=_RANGE_READ_LIMIT,
        sort_by=normalized_sort_by,
        sort_dir=normalized_sort_dir,
        read_filter=normalized_read_filter,
        star_only=normalized_star_only,
        selected_tag=normalized_tag,
        search_query=normalized_query,
        # Only feed_url/id/read/link are used below (anchor matching, the
        # premiere guard) — skip thumbnails/tags/display-prefs/duration for
        # every entry in scope. On a big view this is most of the cost: an
        # 8,472-entry "All Feeds" unread resolve dropped from ~10.4s to ~6.5s.
        enrich=False,
    )

    anchor_index = next(
        (index for index, post in enumerate(posts) if post["feed_url"] == feed_url and post["id"] == entry_id),
        None,
    )

    # If the anchor is missing in the filtered list (e.g. stale read_filter
    # state in the client), retry against the same scoped list with read_filter
    # relaxed to all so range actions still resolve around the selected post.
    if anchor_index is None and normalized_read_filter != "all":
        posts = list_entries_for_feeds(
            filtered_feed_urls,
            limit=_RANGE_READ_LIMIT,
            sort_by=normalized_sort_by,
            sort_dir=normalized_sort_dir,
            read_filter="all",
            star_only=normalized_star_only,
            selected_tag=normalized_tag,
            search_query=normalized_query,
            enrich=False,
        )
        anchor_index = next(
            (index for index, post in enumerate(posts) if post["feed_url"] == feed_url and post["id"] == entry_id),
            None,
        )

    if anchor_index is None:
        message = "Could not find that post in the current view."
    else:
        if direction == "above":
            target_posts = posts[:anchor_index]
        elif direction == "below":
            target_posts = posts[anchor_index + 1 :]
        else:
            target_posts = []

        marked_count = 0
        if target_posts:
            to_sync: list[tuple[str, str]] = []
            with get_reader() as reader:
                for post in target_posts:
                    if post["read"]:
                        continue
                    # A premiere that hasn't aired yet shouldn't be swallowed by
                    # a blanket "read above/below" sweep.
                    if _youtube_unpremiered_video_id(post.get("feed_url"), post.get("link")) is not None:
                        continue
                    try:
                        reader.mark_entry_as_read((post["feed_url"], post["id"]))
                    except Exception:
                        continue
                    to_sync.append((post["feed_url"], post["id"]))
            if to_sync:
                when = datetime.now().isoformat()
                with get_meta_connection() as conn:
                    conn.executemany(
                        """
                        INSERT INTO entry_read_state (feed_url, entry_id, read_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(feed_url, entry_id) DO UPDATE SET read_at = excluded.read_at
                        """,
                        [(fu, eid, when) for fu, eid in to_sync],
                    )
            marked_count = len(to_sync)

        if direction not in {"above", "below"}:
            message = "Invalid range option."
        elif marked_count == 0:
            message = "No unread posts in that range."
        else:
            range_label = "above" if direction == "above" else "below"
            message = f"Marked {marked_count} posts {range_label} as read."

    list_feed_query = f"&list_feed_url={quote_plus(list_feed_url)}" if list_feed_url else ""
    tag_query = f"&tag={quote_plus(normalized_tag)}" if normalized_tag else ""
    sort_query = build_sort_query(sort_by, sort_dir)
    read_filter_query = build_read_filter_query(read_filter)
    star_only_query = build_star_only_query(normalized_star_only)
    resume_read_filter_query = build_resume_read_filter_query(resume_read_filter, active_read_filter=normalized_read_filter)
    entry_query = _entry_query_suffix(feed_url, entry_id)

    if is_async_action_request(request, "lectio-post-range-read"):
        return JSONResponse({"ok": True, "message": message, "feed_url": feed_url, "entry_id": entry_id, "direction": direction})

    return RedirectResponse(
        url=(
            f"/?folder_id={folder_id}{list_feed_query}{tag_query}{sort_query}{read_filter_query}{star_only_query}{resume_read_filter_query}{entry_query}"
            f"&message={quote_plus(message)}"
        ),
        status_code=303,
    )


@router.post("/entries/mark-older-than-read")
def mark_entries_older_than_read(
    request: Request,
    folder_id: int = Form(...),
    max_age_days: int = Form(...),
    list_feed_url: str | None = Form(default=None),
    tag: str | None = Form(default=None),
    sort_by: str | None = Form(default=None),
    sort_dir: str | None = Form(default=None),
    read_filter: str | None = Form(default=None),
    star_only: str | None = Form(default=None),
    resume_read_filter: str | None = Form(default=None),
):
    normalized_tag = normalize_tag_value(tag)
    with get_meta_connection() as conn:
        feed_urls = get_folder_feed_urls(conn, folder_id)
    filtered_feed_urls = filter_feed_urls(feed_urls, list_feed_url)

    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    marked_count, undo_token = _mark_entries_as_read_for_view(
        filtered_feed_urls,
        sort_by=sort_by,
        sort_dir=sort_dir,
        read_filter=read_filter,
        star_only=star_only,
        tag=tag,
        older_than_cutoff=cutoff,
    )
    if marked_count:
        _bump_unread_counts_generation()
        unread_counts_cache.clear()

    list_feed_query = f"&list_feed_url={quote_plus(list_feed_url)}" if list_feed_url else ""
    tag_query = f"&tag={quote_plus(normalized_tag)}" if normalized_tag else ""
    _nrf_mot = normalize_read_filter(read_filter)
    sort_query = build_sort_query(sort_by, sort_dir)
    read_filter_query = build_read_filter_query(read_filter)
    star_only_query = build_star_only_query(star_only)
    resume_read_filter_query = build_resume_read_filter_query(resume_read_filter, active_read_filter=_nrf_mot)
    message = "No unread posts older than that." if marked_count == 0 else f"Marked {marked_count} posts as read."
    if is_async_action_request(request, "lectio-mark-read"):
        return JSONResponse(
            {
                "ok": True,
                "marked": marked_count,
                "max_age_days": max_age_days,
                "message": message,
                "undo_token": undo_token,
            }
        )
    return RedirectResponse(
        url=f"/?folder_id={folder_id}{list_feed_query}{tag_query}{sort_query}{read_filter_query}{star_only_query}{resume_read_filter_query}&message={quote_plus(message)}",
        status_code=303,
    )


@router.post("/entries/undo-mark-unread")
def undo_mark_unread(unread_at: str = Form(...)):
    """Undo a bulk mark-as-unread — the mirror of undo_mark_read.

    Same shape and same window: one shared timestamp per batch is the token, and
    only that batch is put back, so reads either side of it are untouched.
    """
    _bad_token = _undo_token_problem(unread_at)
    if _bad_token is not None:
        return _bad_token

    with get_meta_connection() as conn:
        pairs = [
            (str(r["feed_url"]), str(r["entry_id"]))
            for r in conn.execute("SELECT feed_url, entry_id FROM entry_unread_batch WHERE unread_at = ?", (unread_at,))
        ]
    if not pairs:
        return JSONResponse({"ok": False, "error": "Nothing to undo."}, status_code=404)

    restored = 0
    with get_reader() as reader:
        for feed_url, entry_id in pairs:
            try:
                reader.mark_entry_as_read((feed_url, entry_id))
                restored += 1
            except Exception:  # noqa: BLE001 — entry may have been deleted since
                LOGGER.debug("[undo-mark-unread] could not restore %s in %s", entry_id, feed_url)

    when = datetime.now().isoformat()
    with get_meta_connection() as conn:
        conn.executemany(
            """
            INSERT INTO entry_read_state (feed_url, entry_id, read_at) VALUES (?, ?, ?)
            ON CONFLICT(feed_url, entry_id) DO UPDATE SET read_at = excluded.read_at
            """,
            [(f, e, when) for f, e in pairs],
        )
        conn.execute("DELETE FROM entry_unread_batch WHERE unread_at = ?", (unread_at,))
    _bump_unread_counts_generation()
    unread_counts_cache.clear()
    return JSONResponse({"ok": True, "restored": restored})


@router.post("/entries/undo-mark-read")
def undo_mark_read(read_at: str = Form(...)):
    """Undo a bulk mark-as-read (the toast's Undo button).

    Every bulk mark stamps its whole batch with one shared entry_read_state
    read_at value; that timestamp is the undo token. Restores exactly that
    batch to unread — reads before or after it are untouched. Tokens older
    than the undo window are refused: this is a just-pressed-the-wrong-button
    escape hatch, not history."""
    _bad_token = _undo_token_problem(read_at)
    if _bad_token is not None:
        return _bad_token

    with get_meta_connection() as conn:
        pairs = conn.execute("SELECT feed_url, entry_id FROM entry_read_state WHERE read_at = ?", (read_at,)).fetchall()
    if not pairs:
        return JSONResponse({"ok": False, "error": "Nothing to undo."}, status_code=404)

    restored = 0
    with get_reader() as reader:
        for feed_url, entry_id in pairs:
            try:
                reader.mark_entry_as_unread((str(feed_url), str(entry_id)))
                restored += 1
            except Exception:  # noqa: BLE001 — entry may have been deleted since
                LOGGER.debug("[undo-mark-read] could not restore %s in %s", entry_id, feed_url)
    with get_meta_connection() as conn:
        conn.execute("DELETE FROM entry_read_state WHERE read_at = ?", (read_at,))
    invalidate_unread_counts_cache()
    LOGGER.info("[undo-mark-read] restored %d of %d entries from batch %s", restored, len(pairs), read_at)
    return JSONResponse({"ok": True, "restored": restored})


@router.post("/entries/undo-unstar")
def undo_unstar(unstarred_at: str = Form(...)):
    """Undo an accidental unstar (the toast's Undo button on /entries/saved,
    single-post or bulk — entry_unstar_batch's schema always supported N rows
    sharing one token, this route just didn't loop until the bulk star/unstar
    action gave it a real multi-row caller).

    Same shape and window as undo_mark_read/undo_mark_unread: the shared
    timestamp toggle_entry_saved (or the batch route) stamped is the token.
    Restores each star with its *original* saved_at (recorded alongside the
    token) rather than a fresh one, so it lands back in its actual star-order
    position instead of jumping to "just starred." Writes straight to
    saved_entries rather than going through apply_star_state, so this can't
    re-fire "on star, also send to..." destinations — those are for a genuine
    new star, not a restore.
    """
    _bad_token = _undo_token_problem(unstarred_at)
    if _bad_token is not None:
        return _bad_token

    with get_meta_connection() as conn:
        rows = conn.execute(
            "SELECT feed_url, entry_id, saved_at FROM entry_unstar_batch WHERE unstarred_at = ?",
            (unstarred_at,),
        ).fetchall()
    if not rows:
        return JSONResponse({"ok": False, "error": "Nothing to undo."}, status_code=404)

    restored = 0
    gone = 0
    with get_reader() as reader:
        for row in rows:
            feed_url, entry_id = str(row["feed_url"]), str(row["entry_id"])
            # An untagged Saved Article husk is hard-deleted on unstar
            # (apply_star_state -> _hard_delete_entry), not just unstarred —
            # the entry itself is gone, so restoring the saved_entries row
            # would create exactly the orphan-star class of bug the
            # orphaned-star sweep exists to clean up (a row with no matching
            # reader entry). Refuse that one rather than dangle; the rest of
            # the batch still restores.
            if reader.get_entry((feed_url, entry_id), None) is None:
                gone += 1
                with get_meta_connection() as conn:
                    conn.execute("DELETE FROM entry_unstar_batch WHERE feed_url = ? AND entry_id = ?", (feed_url, entry_id))
                    conn.commit()
                continue

            saved_at = row["saved_at"] or datetime.now().isoformat()
            with get_meta_connection() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id, saved_at) VALUES (?, ?, ?)",
                    (feed_url, entry_id, saved_at),
                )
                conn.execute("DELETE FROM entry_unstar_batch WHERE feed_url = ? AND entry_id = ?", (feed_url, entry_id))
                conn.commit()
            try:
                starred_archive_service.enqueue_archive(feed_url, entry_id)
            except Exception as exc:  # noqa: BLE001 — the star itself is already restored
                LOGGER.warning("starred archive re-enqueue failed for %s/%s: %s", feed_url, entry_id, exc)
            restored += 1

    LOGGER.info("[undo-unstar] restored %d/%d from batch %s", restored, len(rows), unstarred_at)
    if restored == 0:
        return JSONResponse(
            {"ok": False, "error": "Those posts no longer exist — they were removed when unstarred."},
            status_code=410,
        )
    return JSONResponse({"ok": True, "restored": restored, "gone": gone})


@router.post("/entries/mark-newer-than-unread")
def mark_entries_newer_than_unread(
    request: Request,
    min_age_days: int = Form(...),
    folder_id: int = Form(...),
    list_feed_url: str | None = Form(default=None),
    tag: str | None = Form(default=None),
    sort_by: str | None = Form(default=None),
    sort_dir: str | None = Form(default=None),
    read_filter: str | None = Form(default=None),
    star_only: str | None = Form(default=None),
    resume_read_filter: str | None = Form(default=None),
):
    normalized_tag = normalize_tag_value(tag)
    with get_meta_connection() as conn:
        feed_urls = get_folder_feed_urls(conn, folder_id)
    filtered_feed_urls = filter_feed_urls(feed_urls, list_feed_url)

    # Kept posts are left alone. Starred or manually tagged means you already
    # decided about it; dragging it back into the Inbox as though it were new is
    # read-state flattening, which is exactly what made the DeviantArt merge
    # annoying. The "older than" direction has no equivalent problem — marking a
    # kept post READ does not resurface it.
    kept = get_tagged_entry_keys(set(filtered_feed_urls))
    _kept_feed_urls = list(filtered_feed_urls)
    if _kept_feed_urls:
        # Scoped to the feeds in play, the way the tagged half already is. Reading every saved row meant
        # a whole-table scan on a library with thousands of saves, to answer a question about one folder.
        with get_meta_connection() as conn:
            _q = ",".join("?" * len(_kept_feed_urls))
            kept |= {
                (str(r["feed_url"]), str(r["entry_id"]))
                for r in conn.execute(
                    f"SELECT feed_url, entry_id FROM saved_entries WHERE feed_url IN ({_q})",
                    _kept_feed_urls,
                )
            }

    cutoff = datetime.now(timezone.utc) - timedelta(days=min_age_days)
    unmarked_count = 0
    to_delete: list[tuple[str, str]] = []
    with get_reader() as reader:
        for fu in filtered_feed_urls:
            for entry in reader.get_entries(feed=fu, read=True):
                if (entry.feed_url, entry.id) in kept or entry.important:
                    continue
                # Same date basis as the list / optimistic client (published or
                # updated or added) so mark-newer mirrors what the UI un-marks.
                date = entry_effective_date(entry)
                if date is None:
                    continue
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                if date < cutoff:
                    continue
                try:
                    reader.mark_entry_as_unread((entry.feed_url, entry.id))
                except Exception:
                    continue
                to_delete.append((entry.feed_url, entry.id))
                unmarked_count += 1

    undo_token = None
    if to_delete:
        undo_token = datetime.now().isoformat()
        with get_meta_connection() as conn:
            conn.executemany(
                "DELETE FROM entry_read_state WHERE feed_url = ? AND entry_id = ?",
                to_delete,
            )
            conn.executemany(
                """
                INSERT INTO entry_unread_batch (feed_url, entry_id, unread_at)
                VALUES (?, ?, ?)
                ON CONFLICT(feed_url, entry_id) DO UPDATE SET unread_at = excluded.unread_at
                """,
                [(fu, eid, undo_token) for fu, eid in to_delete],
            )
        _bump_unread_counts_generation()
        unread_counts_cache.clear()

    list_feed_query = f"&list_feed_url={quote_plus(list_feed_url)}" if list_feed_url else ""
    tag_query = f"&tag={quote_plus(normalized_tag)}" if normalized_tag else ""
    _nrf_mnu = normalize_read_filter(read_filter)
    sort_query = build_sort_query(sort_by, sort_dir)
    read_filter_query = build_read_filter_query(read_filter)
    star_only_query = build_star_only_query(star_only)
    resume_read_filter_query = build_resume_read_filter_query(resume_read_filter, active_read_filter=_nrf_mnu)
    message = "No read posts newer than that." if unmarked_count == 0 else f"Marked {unmarked_count} posts as unread."
    if is_async_action_request(request, "lectio-mark-read"):
        return JSONResponse(
            {"ok": True, "unmarked": unmarked_count, "min_age_days": min_age_days, "message": message, "undo_token": undo_token}
        )
    return RedirectResponse(
        url=f"/?folder_id={folder_id}{list_feed_query}{tag_query}{sort_query}{read_filter_query}{star_only_query}{resume_read_filter_query}&message={quote_plus(message)}",
        status_code=303,
    )


def _email_full_body(feed_url: str, entry_id: str, link: str, stored: str) -> str:
    """The body a "full article text" email sends. A thin stored body (a teaser-only feed) is swapped for the kept offline copy, or a
    live readability fetch — the same thin test as full-content fetch at ingest. Capped at the proxy tier: the sender is waiting on
    the request. Anything that fails, or isn't richer than what's stored, falls back to the stored body."""
    if _archived_copy_is_plausible(stored) or not link.startswith(("http://", "https://")):
        return stored
    candidate = _resolve_archived_readability_html(feed_url, entry_id) or ""
    if not candidate:
        try:
            _title, candidate = fetch_readability_article(link)
        except Exception as exc:  # noqa: BLE001 — a failed fetch still sends what's stored
            LOGGER.info("email full text: readability fetch failed for %s: %s", link, exc)
            return stored
    return candidate if _reader_copy_is_richer(candidate, stored) else stored


@router.post("/entries/email")
def email_entry(
    request: Request,
    feed_url: str = Form(...),
    entry_id: str = Form(...),
    to_addr: str = Form(...),
    cc_me: bool = Form(False),
    full_text: bool = Form(False),
):
    if not is_email_configured():
        return JSONResponse({"ok": False, "error": "Email not configured."}, status_code=503)

    to_addr = to_addr.strip()
    if not to_addr:
        return JSONResponse({"ok": False, "error": "No recipient address."}, status_code=400)

    # "Cc me" makes the share a repliable thread: set Reply-To to the sender's
    # profile email so a recipient's reply reaches them (the From is the Resend
    # sender domain, which may not accept mail), and Cc the sender a copy. Skip the
    # Cc when the sender would be Cc'ing their own to_addr (Reply-To to self is
    # harmless and still wanted).
    cc_addr: str | None = None
    reply_to: str | None = None
    if cc_me:
        with get_meta_connection() as conn:
            profile_email = get_setting(conn, PROFILE_EMAIL_SETTING_KEY) or ""
        if profile_email:
            reply_to = profile_email
            if profile_email.lower() != to_addr.lower():
                cc_addr = profile_email

    with get_reader() as reader:
        entry = reader.get_entry((feed_url, entry_id), None)

    if not entry:
        return JSONResponse({"ok": False, "error": "Entry not found."}, status_code=404)

    title = entry.title or ""
    link = entry.link or ""
    feed_title = (entry.feed.title if entry.feed else None) or ""

    # Entities are decoded here because the mail builder escapes again — see
    # plain_text_excerpt/plain_text_full.
    excerpt_html = None
    if full_text:
        # Full text wants the article body, not the (often much shorter) feed
        # summary — opposite preference order from the snippet case below.
        raw = (entry.content[0].value if entry.content else "") or entry.summary or ""
        raw = _email_full_body(feed_url, entry_id, link, raw)
        excerpt = html_sanitize.plain_text_full(raw)
        # The HTML part renders the sanitized article body directly — real
        # paragraphs/bold/links/lists, not the plain-text wall above (that's
        # only the text-part fallback). Same allowlist chokepoint the entry
        # pane itself renders with, so the email matches what's on screen.
        if raw:
            excerpt_html = _sanitize_html_allowlist(raw)
    else:
        raw = entry.summary or (entry.content[0].value if entry.content else "")
        excerpt = html_sanitize.plain_text_excerpt(raw)

    ok, error = send_article_email(
        api_key=get_resend_api_key(),
        from_addr=get_resend_from(),
        to_addr=to_addr,
        title=title,
        feed_title=feed_title,
        link=link,
        excerpt=excerpt,
        cc_addr=cc_addr,
        reply_to=reply_to,
        excerpt_html=excerpt_html,
    )
    if ok:
        msg = f"Sent to {to_addr}" + (f" (Cc {cc_addr})" if cc_addr else "")
        return JSONResponse({"ok": True, "message": msg})
    LOGGER.warning("email send failed for %s/%s: %s", feed_url, entry_id, error)
    return JSONResponse({"ok": False, "error": error or "Send failed."}, status_code=500)


@router.post("/entries/instapaper")
def save_to_instapaper(
    feed_url: str = Form(...),
    entry_id: str = Form(...),
):
    username = get_runtime_setting(SETTING_INSTAPAPER_USERNAME).strip()
    password = get_runtime_setting(SETTING_INSTAPAPER_PASSWORD).strip()
    if not (username and password):
        return JSONResponse({"ok": False, "error": "Instapaper not configured."}, status_code=503)

    with get_reader() as reader:
        entry = reader.get_entry((feed_url, entry_id), None)

    if not entry:
        return JSONResponse({"ok": False, "error": "Entry not found."}, status_code=404)

    url = entry.link or ""
    if not url:
        return JSONResponse({"ok": False, "error": "Entry has no URL."}, status_code=400)

    ok, err = _instapaper_save_url(username, password, url, entry.title or "")
    if ok:
        return JSONResponse({"ok": True})
    LOGGER.warning("Instapaper save failed for %s: %s", url, err)
    return JSONResponse({"ok": False, "error": err}, status_code=502)


@router.post("/entries/quire")
def add_to_quire(
    feed_url: str = Form(...),
    entry_id: str = Form(...),
    project_oid: str | None = Form(None),
):
    if not is_quire_connected():
        return JSONResponse({"ok": False, "error": "Quire not connected."}, status_code=503)
    project_oid = (project_oid or "").strip() or quire_project_oid()
    if not project_oid:
        return JSONResponse({"ok": False, "error": "Pick a Quire destination project."}, status_code=503)
    if get_quire_usage_status()["state"] == "blocked":
        return JSONResponse({"ok": False, "error": "Quire rate limit reached — try again shortly."}, status_code=429)
    token = get_quire_user_token()
    if not token:
        return JSONResponse({"ok": False, "error": "Quire session expired — reconnect in Settings."}, status_code=503)

    with get_reader() as reader:
        entry = reader.get_entry((feed_url, entry_id), None)
        feed_title = ""
        if entry:
            try:
                feed_title = str(getattr(reader.get_feed(feed_url), "title", None) or "")
            except Exception:
                feed_title = ""
    if not entry:
        return JSONResponse({"ok": False, "error": "Entry not found."}, status_code=404)

    ok, err = _quire_add_entry(token, project_oid, entry.title or "", entry.link or "", feed_title)
    if ok:
        return JSONResponse({"ok": True})
    LOGGER.warning("Quire add failed for %s: %s", entry.link or entry_id, err)
    return JSONResponse({"ok": False, "error": err}, status_code=502)


@router.get("/entries/pane", response_class=HTMLResponse)
def entry_pane(
    request: Request,
    folder_id: int,
    feed_url: str,
    entry_id: str,
    list_feed_url: str | None = None,
    tag: str | None = None,
    sort_by: str | None = None,
    sort_dir: str | None = None,
    read_filter: str | None = None,
    star_only: str | None = None,
    resume_read_filter: str | None = None,
):
    normalized_tag = normalize_tag_value(tag)
    normalized_sort_by = normalize_sort_by(sort_by)
    normalized_sort_dir = normalize_sort_dir(sort_dir)
    normalized_read_filter = normalize_read_filter(read_filter)
    normalized_star_only = normalize_star_only(star_only)
    normalized_resume_read_filter = normalize_resume_read_filter(resume_read_filter)

    _pane_t0 = time.monotonic()
    selected_entry = get_entry_detail(feed_url, entry_id)
    _detail_ms = int((time.monotonic() - _pane_t0) * 1000)
    if _detail_ms > 500:
        LOGGER.info("[perf] entry_pane: get_entry_detail=%dms feed=%s", _detail_ms, feed_url)
    if selected_entry and not selected_entry["read"]:
        selected_entry["read"] = True
        _mark_entry_read_background(
            feed_url,
            entry_id,
            str(selected_entry.get("title") or ""),
            str(selected_entry.get("link") or ""),
            str(selected_entry.get("feed_title") or ""),
        )

    # Build a tiny feed_url→folder_id map for the entry pane's feed-name link
    # so it lands in the feed's actual containing folder.
    feed_to_folder: dict[str, int] = {}
    with get_meta_connection() as conn:
        snapshot = get_meta_structure_snapshot(conn)
    direct = cast(dict[int, list[str]], snapshot["direct_feed_urls_by_folder"])
    for fid, urls in direct.items():
        for url in urls:
            feed_to_folder[url] = fid

    return templates.TemplateResponse(
        request,
        "_entry_pane.html",
        {
            "selected_folder_id": folder_id,
            "selected_feed_url": list_feed_url,
            "selected_tag": normalized_tag,
            "selected_sort_by": normalized_sort_by,
            "selected_sort_dir": normalized_sort_dir,
            "selected_read_filter": normalized_read_filter,
            "selected_star_only": normalized_star_only,
            "selected_resume_read_filter": normalized_resume_read_filter,
            "selected_entry": selected_entry,
            "feed_to_folder": feed_to_folder,
            "unsubscribed_feed_urls": unsubscribed_feed_urls_among([selected_entry.get("feed_url")] if selected_entry else []),
            "email_configured": is_email_configured(),
            "email_to_default": _get_email_to_default(),
            "instapaper_configured": is_instapaper_configured(),
            "pinterest_connected": pinterest_oauth_connected(),
            "quire_configured": is_quire_configured(),
            "reddit_connected": reddit_connected(),
        },
        headers={"Cache-Control": "no-store"},
    )


def _get_email_to_default() -> str:
    if not is_email_configured():
        return ""
    with get_meta_connection() as conn:
        return get_setting(conn, EMAIL_TO_SETTING_KEY) or ""
