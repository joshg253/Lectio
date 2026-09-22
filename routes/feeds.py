"""Feed-management surface (Plan.md's main.py/index.html breakup, Stage 8 of
the route-by-URL-prefix split -- the biggest single cluster at 60 routes,
scoped into its own A-E sub-stages so it doesn't land as one huge diff).

**This file is not done yet.** Sub-stages A and B (folder CRUD + tree reads,
feed discovery/add flow) are the only parts that exist so far -- sub-stages
C-E (display/thumbnail strategy config, network/fetch settings + lifecycle,
and tags/attachments/curation/bulk ops) each add more routes to this same
module in later tasks. Don't assume the routes below are the final contents.

Stage 8A -- folder CRUD + tree reads, 10 routes: `POST /api/folders`,
`POST /folders`, `POST /folders/rename`, `POST /folders/delete`,
`GET /folders/properties`, `POST /folders/cadence`, `POST /folders/retention`,
`POST /folders/mark-read`, `GET /tree/folder-feeds/{folder_id}`, and
`GET /api/folder-feeds` (the latter two are outliers physically far from the
`/folders*` cluster and from each other -- `/tree/folder-feeds/{folder_id}` is
a sidebar-fragment route, `/api/folder-feeds` backs the automation rule
builder's feed picker -- grouped in here because both are folder-tree reads,
same conceptual area as the rest of this sub-stage). No ordering constraint:
none of these handlers touch `_run_automation_after_refresh` or anything else
from the late `services.automation_rules` import, so this module is imported
alongside the plain `routes.compat_*`/`routes.tags`-style modules.

No helper moved with its route this stage -- everything these 10 routes call
is pre-existing main.py-resident, widely-shared infrastructure (checked across
`routes/*.py`, `scripts/*.py`, and `tests/`, not just main.py), or is
exercised directly as `main.<name>` by a dedicated test file, same precedent
prior stages established: `get_folder_properties` and `delete_folder` are both
called directly by `tests/integration/test_folder_properties_counts.py`,
`tests/integration/test_retention_purge.py`, and
`tests/integration/test_feed_removal_consolidation.py`.
`_FOLDER_CADENCE_LAST_REFRESH_PREFIX` (used by `set_folder_cadence`) is also
read by the still-in-main.py cadence-refresh scheduler. `_mark_entries_as_read_for_view`
(used by `mark_folder_as_read`) has two other main.py-resident callers
(`/feeds/mark-read`, `/entries/mark-older-than-read`, neither in this
sub-stage). All of it stays in main.py and is imported back.

Stage 8B -- feed discovery/add flow, 13 routes: `GET /feeds/discover`,
`GET /feeds/compare`, `POST /feeds`, `GET /scraped-feeds/picker-frame`,
`POST /scraped-feeds/pick`, `POST /scraped-feeds/preview`,
`POST /scraped-feeds`, `POST /scraped-feeds/delete`, `GET /feeds/properties`,
`GET /feeds/suggest-migration`, `POST /feeds/set-user-title`,
`POST /feeds/fix-url-titles`, `GET /feeds/lazy-titles`. Same no-ordering-
constraint story as 8A. This cluster is not a thin wrapper -- `create_feed`
(`POST /feeds`) and the scraped-feeds routes hold substantial inline logic
(dev.to/DeviantArt add-feed branches, discovery-result handling, the page-feed
scraper flow), closer to Stage 6's saved-articles cluster than Stage 2's thin
compat surfaces.

Two small single-route-only helpers moved with their route, having no caller
anywhere else: `_is_youtube_url` (with `create_feed`) and `_site_name_from_feed_url`
plus its `_FEEDBURNER_HOSTS`/`_LAZY_TITLE_WORDS` constants (with `get_lazy_titles`).
Everything else stays in main.py and is imported back, each for a reason found
by the usual `routes/*.py` + `scripts/*.py` + `tests/` three-way grep:
`_compare_one_feed` and `_guid_type` (its own dependency) are exercised
directly as `main._compare_one_feed` by `tests/integration/test_compare_one_feed.py`;
`_devto_config_from_form` is shared with `routes/system.py`'s dev.to
config-update route; `_site_name_from_subtitle` (with its `_SUBTITLE_SEPARATORS`
constant) is exercised directly by `tests/integration/test_feed_removal_consolidation.py`;
`_is_youtube_host` is called directly by `scripts/migrate_tag_as_keep.py` as
`main._is_youtube_host`; `get_feed_properties`, `add_feed_to_folder`,
`assume_https_if_schemeless`, and `build_source_proxy_response` are each
exercised directly as `main.<name>` by their own dedicated test files;
`flag_browser_ua_feed`/`_invalidate_browser_ua_cache` are also called from the
still-in-main.py scheduled-refresh path and the not-yet-moved `/feeds/browser-ua`
route (sub-stage D); `get_deviantart_user_token`/`get_deviantart_credentials`/
`_apply_deviantart_image_strategy` are shared with `routes/integrations_deviantart.py`
and (the credentials getter) `routes/settings.py`; `get_reader`, `_run_in_user_context`,
`tenancy`, and `feed_refresh_service` are widely-shared infrastructure already
imported by several other `routes/*.py` modules; `feed_title_map_cache`/
`feed_title_map_cache_lock` are the same state.py-sourced cache singletons
`routes/feeds.py` already re-exports a sibling pair of (`unread_counts_cache*`)
from Stage 8A.

Two test files needed retargeting for the usual "handler registered directly
as `main.<name>` on a bare test `FastAPI()` app" gotcha:
`tests/integration/test_discover_feed_route.py` (-> `routes.feeds.discover_feed_route`)
and `tests/integration/test_suggest_feed_migration_route.py`
(-> `routes.feeds.suggest_feed_migration_route`), neither needing an
additional copied-reference patch since both tests patch module objects
(`services.feed_discovery`) rather than a name `create_feed`/`discover_feed_route`
copied out of `main` at import time. `tests/integration/test_force_subscribe.py`
hit both gotchas together: it registers `main.create_feed` directly (retargeted
to `routes.feeds.create_feed`) and separately monkeypatches
`main.discover_feed_urls_ex`/`main.add_feed_to_folder`, which no longer reaches
`routes.feeds`'s own copies of those names -- both patches now also target
`routes.feeds`. `tests/integration/test_feed_removal_consolidation.py` called
`main.get_lazy_titles()` directly in two helper methods; retargeted to
`routes.feeds.get_lazy_titles()`.
"""

from __future__ import annotations

import re
import threading
from typing import cast
from urllib.parse import quote_plus, urlparse

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from main import (
    _FOLDER_CADENCE_LAST_REFRESH_PREFIX,
    LOGGER,
    UNCATEGORIZED_FOLDER_ID,
    FeedInFolder,
    _apply_deviantart_image_strategy,
    _bump_unread_counts_generation,
    _compare_one_feed,
    _devto_config_from_form,
    _disambiguate_feed_titles,
    _invalidate_browser_ua_cache,
    _is_youtube_host,
    _mark_entries_as_read_for_view,
    _run_in_user_context,
    _site_name_from_subtitle,
    add_feed_to_folder,
    assume_https_if_schemeless,
    build_read_filter_query,
    build_resume_read_filter_query,
    build_sort_query,
    build_source_proxy_response,
    build_star_only_query,
    delete_folder,
    deviantart_service,
    devto_service,
    discover_feed_urls_ex,
    feed_discovery,
    feed_refresh_service,
    feed_title_map_cache,
    feed_title_map_cache_lock,
    flag_browser_ua_feed,
    get_all_feed_urls,
    get_all_reader_feed_urls,
    get_deviantart_credentials,
    get_deviantart_user_token,
    get_disabled_feed_urls,
    get_favicon_url,
    get_feed_properties,
    get_feed_title_map,
    get_folder_feed_urls,
    get_folder_properties,
    get_meta_connection,
    get_meta_structure_snapshot,
    get_problematic_feeds_cached,
    get_push_active_feed_urls,
    get_reader,
    get_root_folder_id,
    get_setting,
    get_unread_counts_by_feed,
    invalidate_meta_structure_cache,
    is_async_action_request,
    normalize_read_filter,
    normalize_sort_by,
    normalize_sort_dir,
    normalize_star_only,
    normalize_tag_value,
    saved_articles_service,
    scraper_service,
    set_setting,
    sort_setting_keys,
    templates,
    tenancy,
    unread_counts_cache,
    unread_counts_cache_lock,
    url_guard,
)

router = APIRouter()


@router.post("/api/folders")
def api_create_folder(name: str = Form(...)):
    name = name.strip()
    if not name:
        return JSONResponse({"ok": False, "error": "Name required"}, status_code=400)
    with get_meta_connection() as conn:
        root_id = get_root_folder_id(conn)
        conn.execute(
            "INSERT OR IGNORE INTO folders (name, parent_id) VALUES (?, ?)",
            (name, root_id),
        )
        row = conn.execute(
            "SELECT id FROM folders WHERE name = ? AND parent_id = ?",
            (name, root_id),
        ).fetchone()
        folder_id = int(row["id"]) if row else root_id
    invalidate_meta_structure_cache()
    return JSONResponse({"ok": True, "id": folder_id, "name": name})


@router.post("/folders")
def create_folder(name: str = Form(...)):
    with get_meta_connection() as conn:
        root_id = get_root_folder_id(conn)
        conn.execute(
            "INSERT OR IGNORE INTO folders (name, parent_id) VALUES (?, ?)",
            (name.strip(), root_id),
        )
        row = conn.execute(
            "SELECT id FROM folders WHERE name = ? AND parent_id = ?",
            (name.strip(), root_id),
        ).fetchone()
        target_id = root_id if not row else int(row["id"])
    invalidate_meta_structure_cache()
    return RedirectResponse(url=f"/?folder_id={target_id}", status_code=303)


@router.post("/folders/rename")
def rename_folder_route(folder_id: int = Form(...), name: str = Form(...)):
    with get_meta_connection() as conn:
        conn.execute(
            "UPDATE folders SET name = ? WHERE id = ?",
            (name.strip(), folder_id),
        )
    invalidate_meta_structure_cache()
    return RedirectResponse(url=f"/?folder_id={folder_id}", status_code=303)


@router.post("/folders/delete")
def delete_folder_route(
    folder_id: int = Form(...),
    feed_action: str = Form("unsub"),
    move_to_folder_id: int | None = Form(None),
):
    root_id = None
    try:
        with get_meta_connection() as conn:
            root_id = get_root_folder_id(conn)
        deleted_folders, deleted_feeds, moved_feeds = delete_folder(folder_id, feed_action=feed_action, move_to_folder_id=move_to_folder_id)
        if feed_action == "move":
            message = f"Deleted {deleted_folders} folder(s). Moved {moved_feeds} feed(s)."
        else:
            message = f"Deleted {deleted_folders} folder(s). Removed {deleted_feeds} feed subscription(s)."
    except ValueError as exc:
        message = str(exc)
        if root_id is None:
            with get_meta_connection() as conn:
                root_id = get_root_folder_id(conn)

    if root_id is None:
        with get_meta_connection() as conn:
            root_id = get_root_folder_id(conn)

    return RedirectResponse(
        url=f"/?folder_id={root_id}&message={quote_plus(message)}",
        status_code=303,
    )


@router.get("/folders/properties")
def folder_properties(folder_id: int):
    return JSONResponse(get_folder_properties(folder_id))


@router.post("/folders/cadence")
def set_folder_cadence(folder_id: int = Form(...), cadence_minutes: str = Form(...)):
    """Set or clear the per-folder refresh cadence."""
    try:
        minutes = int(cadence_minutes)
        if minutes < 0:
            raise ValueError
    except ValueError:
        return JSONResponse({"ok": False, "error": "cadence_minutes must be a non-negative integer"}, status_code=400)
    with get_meta_connection() as conn:
        conn.execute(
            "UPDATE folders SET cadence_minutes = ? WHERE id = ?",
            (minutes if minutes > 0 else None, folder_id),
        )
        # Clear the last-refresh timestamp so the next cycle picks up the new cadence immediately.
        set_setting(conn, f"{_FOLDER_CADENCE_LAST_REFRESH_PREFIX}{folder_id}", "0")
    return JSONResponse({"ok": True, "cadence_minutes": minutes if minutes > 0 else None})


@router.post("/folders/retention")
def set_folder_retention(folder_id: int = Form(...), retention_days: str = Form(...)):
    """Set or clear the per-folder retention (delete read posts N days after
    read, applied by the nightly maintenance; 0 = keep forever)."""
    try:
        days = int(retention_days)
        if days < 0:
            raise ValueError
    except ValueError:
        return JSONResponse({"ok": False, "error": "retention_days must be a non-negative integer"}, status_code=400)
    with get_meta_connection() as conn:
        conn.execute(
            "UPDATE folders SET retention_days = ? WHERE id = ?",
            (days if days > 0 else None, folder_id),
        )
    return JSONResponse({"ok": True, "retention_days": days if days > 0 else None})


@router.post("/folders/mark-read")
def mark_folder_as_read(
    request: Request,
    folder_id: int = Form(...),
    tag: str | None = Form(default=None),
    sort_by: str | None = Form(default=None),
    sort_dir: str | None = Form(default=None),
    read_filter: str | None = Form(default=None),
    star_only: str | None = Form(default=None),
    resume_read_filter: str | None = Form(default=None),
):
    normalized_tag = normalize_tag_value(tag)
    normalized_read_filter_mr = normalize_read_filter(read_filter)
    tag_query = f"&tag={quote_plus(normalized_tag)}" if normalized_tag else ""
    sort_query = build_sort_query(sort_by, sort_dir)
    read_filter_query = build_read_filter_query(read_filter)
    star_only_query = build_star_only_query(star_only)
    resume_read_filter_query = build_resume_read_filter_query(resume_read_filter, active_read_filter=normalized_read_filter_mr)
    with get_meta_connection() as conn:
        feed_urls = get_folder_feed_urls(conn, folder_id)

    marked_count, undo_token = _mark_entries_as_read_for_view(
        feed_urls,
        sort_by=sort_by,
        sort_dir=sort_dir,
        read_filter=read_filter,
        star_only=star_only,
        tag=tag,
    )
    with unread_counts_cache_lock:
        _bump_unread_counts_generation()
        unread_counts_cache.clear()
    message = "All posts already read." if marked_count == 0 else f"Marked {marked_count} posts as read."
    if is_async_action_request(request, "lectio-mark-read"):
        return JSONResponse({"ok": True, "marked": marked_count, "message": message, "undo_token": undo_token})
    return RedirectResponse(
        url=f"/?folder_id={folder_id}{tag_query}{sort_query}{read_filter_query}{star_only_query}{resume_read_filter_query}&message={quote_plus(message)}",
        status_code=303,
    )


@router.get("/tree/folder-feeds/{folder_id}")
def tree_folder_feeds_fragment(request: Request, folder_id: int, star_only: str | None = Query(default=None)) -> Response:
    """One folder's sidebar feed rows (<li> fragment).

    The sidebar renders folder rows only; each collapsed folder's feed list is
    an empty <ul data-lazy-feeds> filled from here on first expand — inlining
    every folder's rows costs megabytes at thousands of feeds. Link query
    fragments (sort / read filter) are rebuilt from the remembered preferences
    and the read-filter cookie — the same sources a fresh full render uses —
    and the SPA re-stamps them from live state at click time anyway.
    """
    with get_meta_connection() as conn:
        snapshot = get_meta_structure_snapshot(conn)
        direct_feed_urls_by_folder = cast(dict[int, list[str]], snapshot["direct_feed_urls_by_folder"])
        all_feed_urls = cast(set[str], snapshot["all_feed_urls"])
        disabled_feed_urls = get_disabled_feed_urls(conn)
        problematic_feeds = get_problematic_feeds_cached(conn)
        # Same per-scope split as a full render: these rows are links into
        # whichever view the sidebar is currently showing, so stamping them with
        # the other scope's order would make one click silently re-sort.
        _star_only_bool = normalize_star_only(star_only)
        _sb_key, _sd_key = sort_setting_keys(_star_only_bool)
        # allow_starred has to match the scope actually being read here, same
        # as the full render's own read (index.html's home route) already
        # does — otherwise a remembered "starred"/"size" (only meaningful in
        # the Saved scope, which this fragment is when star_only is set) gets
        # silently normalized back to the default on every read, same shape
        # of bug as the one that let a remembered "starred" destroy itself.
        sort_by = normalize_sort_by(get_setting(conn, _sb_key), allow_starred=_star_only_bool)
        sort_dir = normalize_sort_dir(get_setting(conn, _sd_key))

    if folder_id == UNCATEGORIZED_FOLDER_ID:
        urls = sorted(get_all_reader_feed_urls() - all_feed_urls - {saved_articles_service.SAVED_FEED_URL})
    else:
        urls = direct_feed_urls_by_folder.get(folder_id, [])

    error_feed_urls: set[str] = {cast(str, pf["feed_url"]) for pf in problematic_feeds if not pf.get("acknowledged_at")}
    feed_title_map = get_feed_title_map()
    unread_counts_by_feed = get_unread_counts_by_feed()
    folder_feeds = [
        FeedInFolder(
            url=url,
            title=feed_title_map.get(url, url),
            icon_url=get_favicon_url(url),
            unread_count=unread_counts_by_feed.get(url, 0),
            has_error=url in error_feed_urls,
        )
        for url in urls
        if url not in disabled_feed_urls  # sidebar shows active feeds only
    ]
    folder_feeds.sort(key=lambda f: f.title.casefold())
    _disambiguate_feed_titles(folder_feeds)

    # Same compact query fragments as the tree links in index.html: omit
    # default values, never carry star mode.
    read_filter = normalize_read_filter(request.cookies.get("lectio_read_filter"))
    tree_read_filter = "all" if read_filter == "history" else read_filter
    _tree_sq = (f"&sort_by={sort_by}" if sort_by != "post" else "") + (f"&sort_dir={sort_dir}" if sort_dir != "asc" else "")
    _tree_rfq = f"&read_filter={tree_read_filter}" if tree_read_filter != "all" else ""
    html = templates.env.get_template("_tree_folder_feeds.html").render(
        {
            "row": {"id": folder_id},
            "folder_feeds": folder_feeds,
            "selected_feed_url": None,
            "push_feed_urls": get_push_active_feed_urls(),
            "_tree_sq": _tree_sq,
            "_tree_rfq": _tree_rfq,
        }
    )
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@router.get("/api/folder-feeds")
def api_folder_feeds(folder_id: str = Query("")):
    """Feeds (url + display title) for the automation editor's feed picker.

    *folder_id* is a single id or a comma-separated list (the rule builder's
    folder picker is multi-select — the feed candidate pool is the union of
    whatever folders are chosen there). Empty, or nothing numeric in the list,
    returns every feed. Server-backed on purpose: the picker used to scrape
    the sidebar's feed links, which don't exist at all in Saved mode — typing
    showed no feeds."""
    titles = get_feed_title_map()
    ids = [f for f in folder_id.split(",") if f.strip().lstrip("-").isdigit()]
    with get_meta_connection() as conn:
        if ids:
            urls: set[str] = set()
            for fid in ids:
                urls |= get_folder_feed_urls(conn, int(fid))
        else:
            urls = get_all_feed_urls(conn)
    feeds = [{"url": u, "title": titles.get(u, u)} for u in urls if not saved_articles_service.is_saved_articles_feed(u)]
    feeds.sort(key=lambda f: f["title"].lower())
    return JSONResponse({"feeds": feeds})


@router.get("/feeds/discover")
def discover_feed_route(url: str = Query(...)):
    from services.feed_discovery import probe_url as _probe_url

    # Schemeless paste, assumed https — otherwise the SSRF guard rejects it
    # with a misleading "private target" message instead of actually probing it.
    url = assume_https_if_schemeless(url.strip())
    return JSONResponse(_probe_url(url))


@router.get("/feeds/compare")
def compare_feeds_route(urls: list[str] = Query(..., alias="url")):
    from concurrent.futures import ThreadPoolExecutor

    capped = [u.strip() for u in urls[:6]]
    with ThreadPoolExecutor(max_workers=len(capped)) as ex:
        results = list(ex.map(_compare_one_feed, capped))
    return JSONResponse(results)


def _is_youtube_url(url: str) -> bool:
    try:
        return _is_youtube_host(urlparse(url).netloc)
    except ValueError:  # urlparse rejects some malformed inputs (e.g. bad IPv6)
        return False


@router.post("/feeds")
def create_feed(
    feed_url: str = Form(...),
    folder_id: int = Form(...),
    devto_tag: str = Form(""),
    devto_top_days: str = Form(""),
    devto_english_only: str = Form(""),
    devto_min_reactions: str = Form(""),
    devto_tags_exclude: str = Form(""),
    force: int = Form(0),
):
    url = feed_url.strip()
    target_url = url
    auto_discovered = False

    # dev.to front-page/tag URLs become filtered API-backed feeds (the raw RSS is
    # an unfiltered firehose). The Add-Feed dialog shows the filter fields when it
    # detects a dev.to URL; a bare POST without them still works with defaults.
    devto_parsed = devto_service.parse_devto_url(url)
    if devto_parsed:
        config = _devto_config_from_form(devto_tag, devto_top_days, devto_english_only, devto_min_reactions, devto_tags_exclude)
        config["tag"] = config["tag"] or devto_parsed.get("tag") or ""
        try:
            with get_meta_connection() as conn:
                with get_reader() as reader:
                    _fid, file_url = devto_service.create_devto_feed(conn, reader, config)
                conn.execute(
                    "INSERT OR IGNORE INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)",
                    (folder_id, file_url),
                )
            invalidate_meta_structure_cache()
            msg = f"dev.to feed added ({devto_service.default_title(config)})."
        except devto_service.DevToRateLimited:
            msg = "dev.to rate limit — try again in a bit."
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("[devto] add failed for %s: %s", url, exc)
            msg = f"dev.to add failed: {exc}"
        return RedirectResponse(url=f"/?folder_id={folder_id}&message={quote_plus(msg)}", status_code=303)

    # DeviantArt: when connected, "adding" an artist just Watches them on DeviantArt
    # — their posts arrive via the single combined Watch feed, so we don't create a
    # per-artist local feed. (If not connected, fall back to a standalone gallery feed.)
    da_username = deviantart_service.username_from_url(url)
    if da_username:
        token = get_deviantart_user_token()
        if token:
            try:
                ok, detail = deviantart_service.watch_user(token, da_username)
                msg = (
                    f"Now watching {da_username} on DeviantArt — new posts appear in your Watch feed."
                    if ok
                    else f"Couldn't watch {da_username}: {detail}"
                )
            except deviantart_service.DeviantArtRateLimited:
                msg = "DeviantArt rate limit — try again in a bit."
            except Exception as exc:  # noqa: BLE001
                msg = f"DeviantArt watch failed: {exc}"
            return RedirectResponse(url=f"/?folder_id={folder_id}&message={quote_plus(msg)}", status_code=303)
        # Not connected → standalone gallery feed (best effort with app creds).
        cid, secret = get_deviantart_credentials()
        if not cid or not secret:
            return RedirectResponse(
                url=(f"/?folder_id={folder_id}&message={quote_plus('Connect your DeviantArt account in Settings first.')}"),
                status_code=303,
            )
        try:
            with get_meta_connection() as conn:
                with get_reader() as reader:
                    _fid, file_url = deviantart_service.create_deviantart_feed(conn, reader, da_username, cid, secret)
                conn.execute(
                    "INSERT OR IGNORE INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)",
                    (folder_id, file_url),
                )
                _apply_deviantart_image_strategy(conn, file_url)
            invalidate_meta_structure_cache()
            msg = f"DeviantArt gallery added ({da_username})."
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("[deviantart] add failed for %s: %s", da_username, exc)
            msg = f"DeviantArt add failed: {exc}"
        return RedirectResponse(url=f"/?folder_id={folder_id}&message={quote_plus(msg)}", status_code=303)

    # For non-YouTube URLs, probe whether the URL is a feed and run
    # auto-discovery if it looks like a webpage instead.
    discovery_escalated = False
    if force and not _is_youtube_url(url):
        # Force skips discovery, and discovery is where the SSRF guard runs.
        # Nothing else on this path checks, so assert it directly: a forced
        # subscribe is an override of a SITE's refusal, never of ours.
        try:
            url_guard.ensure_safe_outbound_url(url)
        except Exception:  # noqa: BLE001 — guard refusal, not a transport error
            # int() is a no-op at runtime (FastAPI has already coerced the form
            # field and rejected anything non-numeric with a 422), but it makes
            # the barrier explicit: the redirect target is a fixed path, and the
            # only interpolated value is provably a number, so nothing here can
            # carry a scheme or host. CodeQL does not model the coercion and
            # reads the raw parameter as remote input (py/url-redirection).
            return RedirectResponse(
                url=(
                    "/?folder_id="
                    + str(int(folder_id))
                    + "&message="
                    + quote_plus("That address is not allowed (private/loopback target).")
                ),
                status_code=303,
            )
    if not _is_youtube_url(url) and not force:
        candidates, discovery_escalated = discover_feed_urls_ex(url)
        if not candidates:
            # Why discovery failed decides what we offer. A REFUSAL (403, a
            # timeout, an empty anti-bot response) means we never saw the
            # content, so the URL may well be a feed behind a wall and
            # subscribing anyway is reasonable — it may start working later.
            # A page we fetched FINE that simply has no feed is an article, and
            # subscribing to it produces a husk: a permanently failing "feed"
            # holding whatever gets captured onto it, invisible unless you go
            # looking. 29 of those had accumulated before this distinction
            # existed (see scripts/rehome_article_feeds.py).
            probe = {}
            try:
                from services.feed_discovery import probe_url as _probe_url

                probe = _probe_url(url)
            except Exception:  # noqa: BLE001 — classification only
                probe = {}
            from services.feed_discovery import refusal_is_forceable

            refused = refusal_is_forceable(probe)
            note = "That address could not be read (the site refused us)." if refused else "No RSS/Atom feed found at that URL."
            return RedirectResponse(
                url=(
                    f"/?folder_id={folder_id}"
                    f"&message={quote_plus(note)}"
                    f"&no_rss_url={quote_plus(url)}" + (f"&force_url={quote_plus(url)}" if refused else "")
                ),
                status_code=303,
            )
        target_url = candidates[0]
        auto_discovered = target_url.rstrip("/") != url.rstrip("/")

    message = "Feed added."
    if auto_discovered:
        message = f"Feed added (discovered from {url})."
    try:
        target_url = add_feed_to_folder(target_url, folder_id)
        # If the feed was only reachable with a browser identity, flag it so
        # reader's refresh fetch escalates too (otherwise it subscribes but never
        # updates). Good-citizen: only after an honest fetch was refused.
        if discovery_escalated:
            with get_meta_connection() as conn:
                flag_browser_ua_feed(conn, target_url, reason="discovery refused honest UA")
            _invalidate_browser_ua_cache()
            message += " (using browser identity — this site blocks default clients.)"
        # Fetch the feed's entries in the background so Add Feed returns
        # immediately. The first refresh can take 10-30s (network + parse +
        # per-entry processing); blocking on it made the dialog spin long
        # enough that users assumed it failed and re-added. The feed shows in
        # the sidebar right away; its entries populate a moment later (and the
        # scheduled refresh would catch it regardless).
        threading.Thread(
            target=_run_in_user_context,
            args=(tenancy.current_user_id(), feed_refresh_service.update_feeds, [target_url]),
            daemon=True,
        ).start()
    except Exception as exc:
        message = f"Feed add failed: {exc}"
        return RedirectResponse(
            url=f"/?folder_id={folder_id}&message={quote_plus(message)}",
            status_code=303,
        )
    # Open the newly-added feed so its identity is obvious immediately (catching
    # a wrong auto-discovery, e.g. a tag page that resolved to the site feed) and
    # it's usable at once — e.g. as a move target while filing. read_filter=all
    # since a brand-new feed's posts are unread anyway and the user wants to see
    # what landed.
    return RedirectResponse(
        url=(f"/?folder_id={folder_id}&list_feed_url={quote_plus(target_url)}&read_filter=all&message={quote_plus(message)}"),
        status_code=303,
    )


@router.get("/scraped-feeds/picker-frame")
def scraped_feed_picker_frame(url: str):
    """Sanitized, same-origin proxy of the source page with the link picker
    injected, for embedding in the Add-Feed modal iframe."""
    return build_source_proxy_response(url, picker=True)


@router.post("/scraped-feeds/pick")
def pick_scraped_feed_selector_route(
    source_url: str = Form(...),
    href: str = Form(...),
):
    """Derive a link-list selector from a link the user clicked in the picker."""
    source_url = source_url.strip()
    href = href.strip()
    if not source_url or not href:
        return JSONResponse({"error": "URL and href required"}, status_code=400)
    try:
        result = scraper_service.pick_page_feed_selector(source_url, href)
    except Exception as exc:
        LOGGER.warning("[scraper] pick failed for %s: %s", source_url, exc)
        return JSONResponse({"error": "Could not fetch or parse that page."}, status_code=502)
    if not result:
        return JSONResponse({"error": "No selector could be derived for that link."}, status_code=404)
    return JSONResponse(result)


@router.post("/scraped-feeds/preview")
def preview_scraped_feed_route(
    source_url: str = Form(...),
    mode: str = Form(default="link_list"),
    selector: str = Form(default=""),
):
    """Preview a page feed's extracted items / suggested selectors before creating it."""
    source_url = source_url.strip()
    if not source_url:
        return JSONResponse({"error": "URL required"}, status_code=400)
    if mode not in ("change_detect", "link_list"):
        mode = "link_list"
    try:
        result = scraper_service.preview_page_feed(source_url, mode, selector.strip() or None)
    except Exception as exc:
        LOGGER.warning("[scraper] preview failed for %s: %s", source_url, exc)
        return JSONResponse({"error": "Could not fetch or parse that page."}, status_code=502)
    return JSONResponse(result)


@router.post("/scraped-feeds")
def create_scraped_feed_route(
    source_url: str = Form(...),
    mode: str = Form(...),
    selector: str = Form(default=""),
    feed_title: str = Form(default=""),
    folder_id: int | None = Form(default=None),
    backfill: str = Form(default=""),
    content_selector: str = Form(default=""),
):
    source_url = source_url.strip()
    if not source_url:
        return RedirectResponse(url="/?message=URL+required", status_code=303)
    if mode not in ("change_detect", "link_list"):
        mode = "change_detect"

    with get_meta_connection() as conn:
        target_folder_id = folder_id or get_root_folder_id(conn)

    try:
        with get_meta_connection() as conn:
            with get_reader() as reader:
                feed_id, file_url = scraper_service.create_scraped_feed(
                    conn,
                    reader,
                    source_url,
                    mode,
                    selector.strip() or None,
                    feed_title.strip() or None,
                    backfill=backfill in ("1", "true", "on", "yes"),
                    content_selector=content_selector.strip() or None,
                )
            conn.execute(
                "INSERT OR IGNORE INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)",
                (target_folder_id, file_url),
            )
    except Exception as exc:
        LOGGER.warning("[scraper] create failed for %s: %s", source_url, exc)
        return RedirectResponse(
            url=f"/?folder_id={target_folder_id}&message={quote_plus(f'Page feed failed: {exc}')}",
            status_code=303,
        )

    invalidate_meta_structure_cache()
    # Open the new page feed (like Add Feed) so it's confirmed and immediately
    # usable, rather than dropping back on the folder. read_filter=all since a
    # fresh feed's items are unread and the user wants to see what it scraped.
    return RedirectResponse(
        url=(
            f"/?folder_id={target_folder_id}&list_feed_url={quote_plus(file_url)}"
            f"&read_filter=all&message={quote_plus('Page feed created.')}"
        ),
        status_code=303,
    )


@router.post("/scraped-feeds/delete")
def delete_scraped_feed_route(
    feed_id: str = Form(...),
    folder_id: int = Form(...),
):
    with get_meta_connection() as conn:
        file_url = scraper_service.feed_file_url(feed_id)
        conn.execute("DELETE FROM folder_feeds WHERE feed_url = ?", (file_url,))
        with get_reader() as reader:
            scraper_service.delete_scraped_feed(conn, reader, feed_id)
    invalidate_meta_structure_cache()
    return RedirectResponse(
        url=f"/?folder_id={folder_id}&message={quote_plus('Page feed removed.')}",
        status_code=303,
    )


@router.get("/feeds/properties")
def feed_properties(feed_url: str):
    return JSONResponse(get_feed_properties(feed_url))


@router.get("/feeds/suggest-migration")
def suggest_feed_migration_route(feed_url: str):
    """A one-click "Suggest fix" for a failing feed on a known dead-end host
    (currently FeedBurner). Never applies anything — the caller pre-fills the
    Change URL field with the candidate and the existing verified flow there
    takes it from there."""
    result = feed_discovery.suggest_feed_migration(feed_url)
    feeds = result.get("feeds") or []
    if not feeds:
        return JSONResponse({"ok": False, "message": result.get("message") or "No suggestion found."})
    candidate = feeds[0]
    return JSONResponse({"ok": True, "candidate_url": candidate["url"], "candidate_title": candidate.get("title")})


@router.post("/feeds/set-user-title")
def set_feed_user_title_route(feed_url: str = Form(...), user_title: str = Form(...)):
    with get_reader() as reader:
        title_to_set = user_title.strip() or None
        reader.set_feed_user_title(feed_url, title_to_set)
    with feed_title_map_cache_lock:
        feed_title_map_cache.clear()
    return JSONResponse({"ok": True, "user_title": user_title.strip() or None})


@router.post("/feeds/fix-url-titles")
def fix_url_titles():
    """Find feeds whose display title is still a raw URL and queue them for refresh."""
    with get_reader() as reader:
        stale_urls = [
            f.url
            for f in reader.get_feeds()
            if not (f.resolved_title or f.title) or (f.resolved_title or f.title or "").lower().startswith("http")
        ]
    if stale_urls:
        threading.Thread(
            target=_run_in_user_context,
            args=(tenancy.current_user_id(), feed_refresh_service.update_feeds, stale_urls),
            daemon=True,
            name="fix-url-titles",
        ).start()
    return JSONResponse({"queued": len(stale_urls)})


# Generic, lazy feed titles -- "News", "Updates", etc -- that tell you
# nothing about which site they came from once several show up side by side
# in a folder or the unread list. Reported 2026-08-10. Deliberately a small,
# exact-match denylist rather than a length heuristic: a short but
# meaningful title ("Kotaku", "XKCD") must never get flagged.
_LAZY_TITLE_WORDS = {
    "news",
    "update",
    "updates",
    "blog",
    "feed",
    "feeds",
    "rss",
    "article",
    "articles",
    "post",
    "posts",
    "latest",
    "home",
    "newsletter",
}


_FEEDBURNER_HOSTS = {"feedburner.com", "feeds.feedburner.com", "feeds2.feedburner.com"}


def _site_name_from_feed_url(url: str) -> str:
    """Guess a human display name for a feed's site from its URL.

    Strips www. and the TLD (best-effort, including two-label ccTLDs like
    .co.uk), then title-cases the remaining hyphen/underscore-separated
    labels. Good enough for a rename *suggestion* a human reviews and can
    edit -- not meant to be DNS-exact.

    FeedBurner is a proxy: every burned feed shares its host regardless of
    the actual site, so a domain-based guess is useless there ("Feedburner"
    for everything, reported 2026-08-10). Falls back to the URL's last path
    segment instead -- FeedBurner's feed slug is normally the site/show name
    (feeds.feedburner.com/concept2 -> "Concept2").
    """
    parsed = urlparse(url)
    host = parsed.netloc.split("@")[-1].split(":")[0].lower()
    if host.startswith("www."):
        host = host[4:]
    if host in _FEEDBURNER_HOSTS:
        path_parts = [p for p in parsed.path.split("/") if p]
        if path_parts:
            core = path_parts[-1]
            words = re.split(r"[-_]+", core)
            return " ".join(w.capitalize() for w in words if w) or host
        return host
    labels = [lbl for lbl in host.split(".") if lbl]
    if len(labels) >= 3 and labels[-2] in {"co", "com", "org", "net", "gov", "ac"}:
        core = labels[-3]
    elif len(labels) >= 2:
        core = labels[-2]
    else:
        core = labels[0] if labels else host
    words = re.split(r"[-_]+", core)
    return " ".join(w.capitalize() for w in words if w) or host


@router.get("/feeds/lazy-titles")
def get_lazy_titles():
    """Find feeds whose title is a generic word (News, Updates, ...) and
    suggest prefixing the site's name -- from the feed's own <subtitle> when
    it has one, else guessed from the domain."""
    with get_meta_connection() as conn:
        rows = conn.execute(
            "SELECT ff.folder_id, ff.feed_url, f.name AS folder_name FROM folder_feeds ff JOIN folders f ON f.id = ff.folder_id"
        ).fetchall()
    url_folders: dict[str, list[dict]] = {}
    for folder_id, feed_url, folder_name in rows:
        url_folders.setdefault(feed_url, []).append({"id": folder_id, "name": folder_name})

    results: list[dict] = []
    with get_reader() as reader:
        for f in reader.get_feeds():
            title = str(f.user_title or f.resolved_title or f.title or "").strip()
            if title.casefold() not in _LAZY_TITLE_WORDS:
                continue
            url = str(f.url)
            site_name = (f.subtitle and _site_name_from_subtitle(str(f.subtitle))) or _site_name_from_feed_url(url)
            results.append(
                {
                    "feed_url": url,
                    "title": title,
                    "suggested_title": f"{site_name} - {title}" if site_name else title,
                    "folders": url_folders.get(url, []),
                }
            )
    results.sort(key=lambda r: r["title"].casefold())
    return JSONResponse({"lazy_titles": results})
