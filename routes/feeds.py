"""Feed-management surface (Plan.md's main.py/index.html breakup, Stage 8 of
the route-by-URL-prefix split -- the biggest single cluster at 60 routes,
scoped into its own A-E sub-stages so it doesn't land as one huge diff).

**This file is not done yet.** Sub-stages A-D (folder CRUD + tree reads, feed
discovery/add flow, display/thumbnail strategy config, network/fetch settings
+ lifecycle) are the only parts that exist so far -- sub-stage E
(tags/attachments/curation/bulk ops) adds more routes to this same module in
a later task. Don't assume the routes below are the final contents.

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

Stage 8C added the feed display/thumbnail strategy config cluster (10 routes, all `POST`): `/feeds/strategy`,
`/feeds/display-prefs`, `/feeds/backfill-hide-shorts`, `/feeds/thumbnail-url`, `/feeds/thumb-crop`,
`/feeds/smart-min-scale`, `/feeds/fill-zoom`, `/feeds/thumb-strategy`, `/feeds/caption-source`,
`/feeds/strategy-refresh`. Same no-ordering-constraint story as 8A/8B. This cluster calls the existing
`lead_image_service` singleton (`services/lead_images.py`) for strategy storage/backfill/comparison, but no code moved
out of that services module or `services/lead_image_plugins.py` -- only the route handlers themselves, exactly as
scoped.

Two small single-route-only pieces moved with their route, having no caller anywhere else: the
`_VALID_MANUAL_STRATEGIES` constant (with `set_feed_image_strategy`) and the whole `upsert_feed_thumb_crop` helper
(with `set_feed_thumb_crop_route`) -- its sibling upsert helpers (`upsert_feed_display_pref`,
`upsert_feed_thumbnail_url`, `upsert_feed_smart_min_scale`, `upsert_feed_fill_zoom`, `upsert_feed_thumb_strategy`) all
stay in main.py and get imported back, each exercised directly as `main.<name>` by its own dedicated test file (the
same "tested directly" precedent Stage 6/8A established), except `upsert_feed_display_pref`, kept for that reason plus
being called from several still-in-main.py display-pref routes far outside this cluster. `_DISPLAY_PREF_KEYS` and
`_VALID_THUMB_CROPS` stay too (both back other main.py-resident code -- `_DISPLAY_PREF_COLS`/`_DISPLAY_PREF_SQLS`
derive from the former at module scope, and the latter is read by two more main.py routes outside this stage) and get
imported back, the latter also backing the moved `upsert_feed_thumb_crop`. `_mark_existing_shorts_read` stays --
shared by two of this stage's own routes plus tested directly as `main.<name>` -- and `youtube_hide_shorts_global`
stays, already shared with `routes/settings.py`. `_pin_feed_thumbnail_bytes`/`_drop_pinned_feed_thumbnail` stay: they
sit inside a larger still-in-main.py thumbnail-pinning block (`_feed_thumb_cache_key`, `/api/feed-thumb`, the parallel
per-entry pinning machinery) that this sub-stage's routes don't own, and `_feed_thumb_cache_key` itself is read by
still-in-main.py code outside this cluster -- only the two functions `set_feed_thumbnail_url_route` calls get imported
back. `format_datetime_for_ui` and `lead_image_service` are widely-shared infrastructure already imported by other
`routes/*.py` modules (`routes/system.py`, `routes/settings.py`).

One test file needed retargeting for the usual "handler registered directly as `main.<name>` on a bare test
`FastAPI()` app" gotcha, combined with the copied-reference variant:
`tests/integration/test_feed_strategy_routes.py` registers `main.set_feed_image_strategy`/`main.set_feed_thumb_strategy_route`
directly (retargeted to `routes.feeds.set_feed_image_strategy`/`routes.feeds.set_feed_thumb_strategy_route`) and
monkeypatches `main.get_meta_connection`/`main.upsert_feed_thumb_strategy`, which no longer reach `routes.feeds`'s own
copies of those names -- both patches now also target `routes.feeds`; its `main.lead_image_service`/`main.threading`/
`main.tenancy` patches needed no change since those patch attributes on shared module/singleton objects, not names
copied at import time. `tests/unit/test_pinned_feed_thumbnail.py` slices main.py's source text by function name for
`set_feed_thumbnail_url_route`; retargeted that one slice to read from `routes/feeds.py` (its end-of-function marker
changed from `"\n@app."` to `"\n@router."` to match) -- its other slices (`_feed_thumb_cache_key`,
`_pin_feed_thumbnail_bytes`, `_evict_img_cache`) stay pointed at main.py since those functions didn't move.

Stage 8D added the feed network/fetch settings + lifecycle cluster (12 routes): `POST /feeds/browser-ua`,
`POST /feeds/proxy`, `POST /feeds/tailscale`, `POST /feeds/flaresolverr`, `POST /feeds/reparse`, `POST /feeds/move`,
`POST /feeds/disable`, `POST /feeds/enable`, `POST /feeds/toggle-updates`, `POST /feeds/change-url`,
`POST /feeds/unsubscribe`, `GET /feeds/curation-count` -- confirmed exactly as scoped, no discrepancy. Same
no-ordering-constraint story as 8A-8C. This is real feed-lifecycle logic (`/feeds/disable`, `/feeds/enable`,
`/feeds/unsubscribe`, `/feeds/change-url`, `/feeds/move` mutate feed state the refresh scheduler, unread counts, and
the rest of the app depend on), not thin wrappers.

Only one genuinely single-route-only helper moved: `feed_curation_counts` (with `feed_curation_count_route`) --
no other caller anywhere, unlike its sibling `feed_curation_items` (backs the not-yet-moved `/feeds/curation-items`,
correctly left alone -- stage E territory despite sitting right next to it in main.py). Everything else this
cluster touches stays in main.py and is imported back, confirmed via the `routes/*.py` + `scripts/*.py` + `tests/`
three-way grep: the `flag_*_feed`/`unflag_*_feed`/`_invalidate_*_feeds_cache` families for browser-UA/proxy/
tailscale/FlareSolverr are each also called from the still-in-main.py escalation chain (the
`_flag_proxy_feed_on_still_blocked`-style callbacks around main.py's fetch-refusal handling) and are individually
tested directly as `main.<name>` by `tests/integration/test_proxy_feeds.py`/`test_browser_ua_feeds.py`/
`test_instance_settings.py` -- exactly the "looks single-route but is a load-bearing shared primitive" trap this
stage was briefed to watch for. `disable_feed`/`enable_feed` stay for the same reason, amplified: both are called
from the DeviantArt watchlist auto-pause path, the scheduled-refresh disable/enable path, and `routes/settings.py`
(already importing them back) in addition to this stage's own routes -- confirmed genuinely shared, not just
routed-through. `_flag_browser_ua_on_refusal` stays, also wired into `feed_refresh_service`'s
`on_fetch_refused` callback at construction time. `move_feed_to_folder` stays, tested directly as `main.<name>` by
`tests/integration/test_single_folder_enforcement.py`. `_normalize_alias_host`/`migrate_feed_host_rewrite` stay,
shared with the still-in-main.py `/feeds/url-rewrites*` cluster (stage E) that sits immediately next to
`change_feed_url_route`'s old location. `get_feeds_needing_replacement`/`invalidate_problematic_feeds_cache`/
`invalidate_unread_counts_cache`/`archive_conn` stay, each widely shared (routes/settings.py, routes/system.py,
routes/saved.py, scripts/*.py, and/or tested directly). `restar_curated_entries`/`drop_all_curation` stay despite
having only one route-caller each, both tested directly as `main.<name>` (and monkeypatched that way) by
`tests/integration/test_feed_removal_consolidation.py`. `purge_orphaned_feed` stays, heavily shared (dedup/combine,
delete_folder, scheduled cleanup, several scripts). `_extract_tag_key`/`MANUAL_TAG_KEY_PREFIX` (needed by the moved
`feed_curation_counts`) stay, both widely used elsewhere in main.py's tag machinery. `FeedRefreshService` and
`FeedNotFoundError`/`FeedExistsError` are imported directly from `services.feed_refresh`/`reader.exceptions` rather
than round-tripped through main.py, same pattern `routes/system.py` and `routes/automation.py` already established
for direct service/library imports.

Two scripts call `change_feed_url_route` directly as a plain function (not through HTTP) rather than any of this
cluster's other routes/helpers: `scripts/fix_reddit_rss_host.py` and `scripts/find_redirecting_feeds.py` (its
`--apply` path). Both retargeted from `main.change_feed_url_route` to `routes.feeds.change_feed_url_route` --
found only by the scripts/*.py leg of the three-way grep, not by main.py or tests/ alone.

Three test files needed retargeting. `tests/integration/test_change_feed_url_validate.py` and
`tests/integration/test_reparse_route.py` both hit the usual "handler registered directly as `main.<name>` on a
bare test `FastAPI()` app" gotcha (`main.change_feed_url_route` -> `routes.feeds.change_feed_url_route`,
`main.reparse_feed_route` -> `routes.feeds.reparse_feed_route`); the latter also hit the copied-reference variant,
since its `monkeypatch.setattr(main, "get_reader", ...)` no longer reaches `routes.feeds`'s own copy -- now patched
on both. `tests/integration/test_feed_removal_consolidation.py` (already importing `routes.feeds` from Stage 8B)
needed its `restar_curated_entries`/`drop_all_curation` monkeypatches (which exercise `/feeds/unsubscribe` through a
real `TestClient(main.app)` post) doubled onto `routes.feeds` as well, for the same copied-reference reason. Two
more test files were reading main.py's raw source text as a live fallback for the `_feed_url_tables` list (the
meta-DB tables `change_feed_url_route` migrates) and needed that slice repointed at `routes/feeds.py`:
`tests/integration/test_undo_unstar.py` and `tests/unit/test_attachment_ext_suppression.py` (the latter's other three
source-slicing assertions, for still-in-main.py attachment-suppression routes, stay pointed at `main.py`).
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import cast
from urllib.parse import quote_plus, urlparse

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from reader.exceptions import FeedExistsError, FeedNotFoundError

from main import (
    _DISPLAY_PREF_KEYS,
    _FOLDER_CADENCE_LAST_REFRESH_PREFIX,
    _VALID_THUMB_CROPS,
    LOGGER,
    MANUAL_TAG_KEY_PREFIX,
    UNCATEGORIZED_FOLDER_ID,
    FeedInFolder,
    _apply_deviantart_image_strategy,
    _bump_unread_counts_generation,
    _compare_one_feed,
    _devto_config_from_form,
    _disambiguate_feed_titles,
    _drop_pinned_feed_thumbnail,
    _extract_tag_key,
    _flag_browser_ua_on_refusal,
    _invalidate_browser_ua_cache,
    _invalidate_flaresolverr_feeds_cache,
    _invalidate_proxy_feeds_cache,
    _invalidate_tailscale_feeds_cache,
    _is_youtube_host,
    _mark_entries_as_read_for_view,
    _mark_existing_shorts_read,
    _normalize_alias_host,
    _pin_feed_thumbnail_bytes,
    _run_in_user_context,
    _site_name_from_subtitle,
    add_feed_to_folder,
    archive_conn,
    assume_https_if_schemeless,
    build_read_filter_query,
    build_resume_read_filter_query,
    build_sort_query,
    build_source_proxy_response,
    build_star_only_query,
    delete_folder,
    deviantart_service,
    devto_service,
    disable_feed,
    discover_feed_urls_ex,
    drop_all_curation,
    enable_feed,
    feed_discovery,
    feed_refresh_service,
    feed_title_map_cache,
    feed_title_map_cache_lock,
    flag_browser_ua_feed,
    flag_flaresolverr_feed,
    flag_proxy_feed,
    flag_tailscale_feed,
    format_datetime_for_ui,
    get_all_feed_urls,
    get_all_reader_feed_urls,
    get_deviantart_credentials,
    get_deviantart_user_token,
    get_disabled_feed_urls,
    get_favicon_url,
    get_feed_properties,
    get_feed_title_map,
    get_feeds_needing_replacement,
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
    invalidate_problematic_feeds_cache,
    invalidate_unread_counts_cache,
    is_async_action_request,
    lead_image_service,
    migrate_feed_host_rewrite,
    move_feed_to_folder,
    normalize_read_filter,
    normalize_sort_by,
    normalize_sort_dir,
    normalize_star_only,
    normalize_tag_value,
    purge_orphaned_feed,
    restar_curated_entries,
    saved_articles_service,
    scraper_service,
    set_setting,
    sort_setting_keys,
    templates,
    tenancy,
    unflag_browser_ua_feed,
    unflag_flaresolverr_feed,
    unflag_proxy_feed,
    unflag_tailscale_feed,
    unread_counts_cache,
    unread_counts_cache_lock,
    upsert_feed_display_pref,
    upsert_feed_fill_zoom,
    upsert_feed_smart_min_scale,
    upsert_feed_thumb_strategy,
    upsert_feed_thumbnail_url,
    url_guard,
    youtube_hide_shorts_global,
)
from services.feed_refresh import FeedRefreshService

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


_VALID_MANUAL_STRATEGIES = {"auto", "inline", "og_scrape", "media_rss", "enclosure", "none", "webcomic", "artwork"}


@router.post("/feeds/strategy")
def set_feed_image_strategy(feed_url: str = Form(...), strategy: str = Form(...)):
    if strategy not in _VALID_MANUAL_STRATEGIES:
        return JSONResponse({"error": "invalid strategy"}, status_code=400)
    if strategy == "auto":
        # Remove manual lock — delete so auto-detection starts fresh.
        try:
            with get_meta_connection() as conn:
                conn.execute("DELETE FROM feed_lead_image_strategy WHERE feed_url = ?", (feed_url,))
        except Exception:
            pass
    else:
        lead_image_service.store_feed_strategy(feed_url, strategy, manual=True)
    # Clear cached images and the strategy comparison grid so entries
    # re-resolve under the new strategy.
    lead_image_service.clear_lead_image_cache(feed_url)
    try:
        with get_meta_connection() as conn:
            conn.execute("DELETE FROM feed_strategy_cache WHERE feed_url = ?", (feed_url,))
    except Exception:
        pass
    # Re-fetch images for recent entries using the new strategy.  Bypass the
    # chunk-backfill semaphore so this isn't silently dropped if another
    # backfill is in flight.
    if strategy not in ("auto", "none"):

        def _refetch(furl: str) -> None:
            try:
                with get_reader() as reader:
                    entries = list(reader.get_entries(feed=furl, limit=50))
                if strategy in ("inline", "artwork", "enclosure"):
                    # _do_backfill_entry_list skips inline/artwork/enclosure (no source-page
                    # fetches needed), so run inline extraction directly using full
                    # Entry objects which carry the feed content and enclosures.
                    for entry in entries:
                        furl_str = str(getattr(entry, "feed_url", "") or "")
                        eid = str(getattr(entry, "id", "") or "")
                        if not furl_str or not eid:
                            continue
                        url = lead_image_service.extract_entry_thumbnail_url(entry)
                        lead_image_service.store_entry_lead_image(furl_str, eid, url)
                else:
                    posts = [
                        {
                            "feed_url": str(getattr(e, "feed_url", "") or ""),
                            "id": str(getattr(e, "id", "") or ""),
                            "link": str(getattr(e, "link", "") or ""),
                        }
                        for e in entries
                    ]
                    lead_image_service._do_backfill_entry_list(posts)
            except Exception:
                pass

        # Capture the request's tenancy user; a raw daemon thread does not
        # inherit contextvars and would otherwise re-fetch as the default user,
        # writing to the wrong DB and leaving this user's cache empty.
        _uid = tenancy.current_user_id()
        threading.Thread(target=_run_in_user_context, args=(_uid, _refetch, feed_url), daemon=True).start()
    return JSONResponse({"ok": True, "strategy": strategy})


@router.post("/feeds/display-prefs")
def set_feed_display_pref_route(
    feed_url: str = Form(...),
    key: str = Form(...),
    value: int = Form(...),
):
    if key not in _DISPLAY_PREF_KEYS:
        return JSONResponse({"error": "invalid key"}, status_code=400)
    with get_meta_connection() as conn:
        upsert_feed_display_pref(conn, feed_url, key, value)
    # Turning Hide Shorts on clears the existing backlog immediately, not just
    # future refreshes.
    marked = 0
    if key == "hide_shorts" and value:
        try:
            marked = _mark_existing_shorts_read({feed_url})
        except Exception:
            LOGGER.exception("[display-prefs] error marking existing shorts read")
    return JSONResponse({"ok": True, "key": key, "value": value, "marked_read": marked})


@router.post("/feeds/backfill-hide-shorts")
def backfill_hide_shorts_route():
    """Re-run the hide-shorts cleanup across all feeds that have hide_shorts=1.

    Useful after the Shorts detection logic is improved (e.g. #shorts hashtag
    or cached duration), so previously-missed Shorts get marked read without
    the user having to re-toggle the pref on every feed."""
    with get_meta_connection() as conn:
        rows = conn.execute("SELECT feed_url FROM feed_display_prefs WHERE hide_shorts = 1").fetchall()
    feed_urls = {str(r["feed_url"]) for r in rows}
    if youtube_hide_shorts_global():
        with get_meta_connection() as conn:
            all_yt = conn.execute(
                "SELECT DISTINCT feed_url FROM feed_display_prefs WHERE feed_url LIKE 'https://www.youtube.com/%'"
            ).fetchall()
        feed_urls |= {str(r["feed_url"]) for r in all_yt}
    try:
        marked = _mark_existing_shorts_read(feed_urls)
    except Exception:
        LOGGER.exception("[backfill-hide-shorts] error")
        return JSONResponse({"error": "backfill failed"}, status_code=500)
    return JSONResponse({"ok": True, "marked": marked})


@router.post("/feeds/thumbnail-url")
def set_feed_thumbnail_url_route(
    feed_url: str = Form(...),
    thumbnail_url: str = Form(default=""),
):
    with get_meta_connection() as conn:
        cleaned = thumbnail_url.strip() or None
        upsert_feed_thumbnail_url(conn, feed_url, cleaned)
        # Pinning a thumbnail URL implies the user wants thumbnails visible —
        # re-enable them if the feed was previously set to Disabled.
        if cleaned:
            upsert_feed_display_pref(conn, feed_url, "show_lead_image_as_thumb", 1)
    pinned = False
    if cleaned and cleaned != "__favicon__":
        pinned = _pin_feed_thumbnail_bytes(feed_url, cleaned)
    else:
        _drop_pinned_feed_thumbnail(feed_url)
    return JSONResponse({"ok": True, "pinned": pinned})


def upsert_feed_thumb_crop(conn: sqlite3.Connection, feed_url: str, crop: str) -> None:
    crop = crop if crop in _VALID_THUMB_CROPS else "cover"
    conn.execute(
        "INSERT INTO feed_display_prefs (feed_url) VALUES (?) ON CONFLICT(feed_url) DO NOTHING",
        (feed_url,),
    )
    conn.execute(
        "UPDATE feed_display_prefs SET thumb_crop = ? WHERE feed_url = ?",
        (crop, feed_url),
    )


@router.post("/feeds/thumb-crop")
def set_feed_thumb_crop_route(
    feed_url: str = Form(...),
    crop: str = Form(...),
):
    if crop not in _VALID_THUMB_CROPS:
        return JSONResponse({"error": "invalid crop"}, status_code=400)
    with get_meta_connection() as conn:
        upsert_feed_thumb_crop(conn, feed_url, crop)
    return JSONResponse({"ok": True})


@router.post("/feeds/smart-min-scale")
def set_feed_smart_min_scale_route(
    feed_url: str = Form(...),
    min_scale: str = Form(default=""),  # empty → clear back to default
):
    parsed: float | None = None
    if min_scale.strip():
        try:
            parsed = float(min_scale)
        except ValueError:
            return JSONResponse({"error": "invalid min_scale"}, status_code=400)
    with get_meta_connection() as conn:
        upsert_feed_smart_min_scale(conn, feed_url, parsed)
    return JSONResponse({"ok": True})


@router.post("/feeds/fill-zoom")
def set_feed_fill_zoom_route(
    feed_url: str = Form(...),
    zoom: str = Form(default=""),  # empty → clear back to default 1.0
):
    parsed: float | None = None
    if zoom.strip():
        try:
            parsed = float(zoom)
        except ValueError:
            return JSONResponse({"error": "invalid zoom"}, status_code=400)
    with get_meta_connection() as conn:
        upsert_feed_fill_zoom(conn, feed_url, parsed)
    return JSONResponse({"ok": True})


@router.post("/feeds/thumb-strategy")
def set_feed_thumb_strategy_route(
    feed_url: str = Form(...),
    strategy: str = Form(default=""),  # Pydantic v2: empty form field → missing, so use default
):
    with get_meta_connection() as conn:
        upsert_feed_thumb_strategy(conn, feed_url, strategy or None)
    # When switching to auto (no override), backfill any entries not yet in
    # entry_lead_images so thumbnails appear without waiting for the next
    # scheduled refresh.  Already-cached entries are skipped by the backfill.
    if not strategy:

        def _backfill(furl: str) -> None:
            try:
                with get_reader() as reader:
                    entries = list(reader.get_entries(feed=furl, limit=50))
                posts = [
                    {
                        "feed_url": str(getattr(e, "feed_url", "") or ""),
                        "id": str(getattr(e, "id", "") or ""),
                        "link": str(getattr(e, "link", "") or ""),
                    }
                    for e in entries
                ]
                lead_image_service._do_backfill_entry_list(posts)
            except Exception:
                pass

        # Re-bind the request's tenancy user inside the daemon thread; otherwise
        # the backfill runs as the default user and writes to the wrong DB.
        _uid = tenancy.current_user_id()
        threading.Thread(target=_run_in_user_context, args=(_uid, _backfill, feed_url), daemon=True).start()
    return JSONResponse({"ok": True})


@router.post("/feeds/caption-source")
def set_feed_caption_source(
    feed_url: str = Form(...),
    source: str = Form(...),
):
    _VALID = {"auto", "alt", "title", "both", "none"}
    if source not in _VALID:
        return JSONResponse({"error": "invalid source"}, status_code=400)
    with get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO feed_display_prefs (feed_url) VALUES (?) ON CONFLICT(feed_url) DO NOTHING",
            (feed_url,),
        )
        conn.execute(
            "UPDATE feed_display_prefs SET caption_source = ? WHERE feed_url = ?",
            (None if source == "auto" else source, feed_url),
        )
    return JSONResponse({"ok": True, "source": source})


@router.post("/feeds/strategy-refresh")
def refresh_feed_strategy_cache_route(
    feed_url: str = Form(...),
    entry_id: str | None = Form(None),
):
    with get_reader() as reader:
        entries = list(reader.get_entries(feed=feed_url, read=None))
    if not entries:
        return JSONResponse({"ok": False, "error": "No entries found for this feed."}, status_code=404)

    sample_entry = None
    if entry_id:
        sample_entry = next((e for e in entries if str(getattr(e, "id", "")) == entry_id), None)

    if sample_entry is None:

        def _best_date(e: object) -> float:
            for attr in ("published", "updated", "added"):
                dt = getattr(e, attr, None)
                if dt:
                    return dt.timestamp()
            return 0.0

        sample_entry = max(entries, key=_best_date)
    strategy_rows = lead_image_service.test_entry_strategies(sample_entry)

    now = time.time()
    formatted_now = format_datetime_for_ui(datetime.fromtimestamp(now, tz=timezone.utc))
    results: list[dict] = []
    with get_meta_connection() as conn:
        for row in strategy_rows:
            conn.execute(
                "INSERT OR REPLACE INTO feed_strategy_cache "
                "(feed_url, strategy, image_url, fetched_at, error, image_alt, image_title) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (feed_url, row["strategy"], row["image_url"], now, row["error"], row.get("image_alt"), row.get("image_title")),
            )
            results.append(
                {
                    "strategy": row["strategy"],
                    "image_url": row["image_url"],
                    "fetched_at": formatted_now,
                    "error": row["error"],
                    "image_alt": row.get("image_alt"),
                    "image_title": row.get("image_title"),
                }
            )

    # Sync the active strategy's alt/title into entry_lead_images so caption_source
    # rendering can read them immediately without waiting for the next feed refresh.
    _active_strat, _, _ = lead_image_service.get_feed_strategy(feed_url)
    _active_row = next(
        (r for r in strategy_rows if r["strategy"] == _active_strat and r.get("image_url")),
        None,
    )
    if _active_row and sample_entry:
        lead_image_service.store_entry_image_alt(
            feed_url,
            str(sample_entry.id),
            _active_row.get("image_alt"),
            title_text=_active_row.get("image_title"),
        )
        lead_image_service.store_entry_lead_image(
            feed_url,
            str(sample_entry.id),
            _active_row["image_url"],
        )

    return JSONResponse({"ok": True, "strategy_cache": results})


@router.post("/feeds/browser-ua")
def set_feed_browser_ua_route(feed_url: str = Form(...), enabled: int = Form(...)):
    """Manually flag/unflag a feed for browser-identity fetches. Auto-set on
    refusal; this lets the user reset a feed back to the honest identity (or force
    it on)."""
    feed_url = feed_url.strip()
    with get_meta_connection() as conn:
        if enabled:
            flag_browser_ua_feed(conn, feed_url, reason="manual")
        else:
            unflag_browser_ua_feed(conn, feed_url)
    _invalidate_browser_ua_cache()
    return JSONResponse({"ok": True, "browser_ua": bool(enabled)})


@router.post("/feeds/proxy")
def set_feed_proxy_route(feed_url: str = Form(...), enabled: int = Form(...)):
    """Manually flag/unflag a feed for as-needed proxy escalation. Auto-set when
    browser-UA was already in play and the fetch still failed; this lets the user
    reset a feed back to direct/browser-UA (or force it on). Only takes effect
    when the resolving mode is as_needed — off/always never consult this flag."""
    feed_url = feed_url.strip()
    with get_meta_connection() as conn:
        if enabled:
            flag_proxy_feed(conn, feed_url, reason="manual")
        else:
            unflag_proxy_feed(conn, feed_url)
    _invalidate_proxy_feeds_cache()
    return JSONResponse({"ok": True, "proxied": bool(enabled)})


@router.post("/feeds/tailscale")
def set_feed_tailscale_route(feed_url: str = Form(...), enabled: int = Form(...)):
    """Manually flag/unflag a feed for last-resort proxy escalation. Auto-set
    when the primary proxy was already in play and the fetch still failed; this
    lets the user reset a feed back to direct/browser-UA/proxy (or force it on).
    Only takes effect when the resolving mode is as_needed AND a last-resort
    backend is configured — same gating as _flag_tailscale_feed_on_still_blocked."""
    feed_url = feed_url.strip()
    with get_meta_connection() as conn:
        if enabled:
            flag_tailscale_feed(conn, feed_url, reason="manual")
        else:
            unflag_tailscale_feed(conn, feed_url)
    _invalidate_tailscale_feeds_cache()
    return JSONResponse({"ok": True, "tailscaled": bool(enabled)})


@router.post("/feeds/flaresolverr")
def set_feed_flaresolverr_route(feed_url: str = Form(...), enabled: int = Form(...)):
    """Manually flag/unflag a feed for FlareSolverr escalation. Auto-set when
    the primary proxy was already in play and the fetch was still specifically
    a bot-challenge; this lets the user reset a feed back to direct/browser-UA
    /proxy (or force it on). Only takes effect when the resolving mode is
    as_needed AND FlareSolverr is configured — same gating as
    _flag_flaresolverr_feed_on_still_blocked."""
    feed_url = feed_url.strip()
    with get_meta_connection() as conn:
        if enabled:
            flag_flaresolverr_feed(conn, feed_url, reason="manual")
        else:
            unflag_flaresolverr_feed(conn, feed_url)
    _invalidate_flaresolverr_feeds_cache()
    return JSONResponse({"ok": True, "flaresolverred": bool(enabled)})


@router.post("/feeds/reparse")
def reparse_feed_route(feed_url: str = Form(...)):
    """Force a full re-fetch + re-parse of one feed to backfill embeds on old
    entries.

    Entries stored before ingest stopped sanitizing feed HTML (see
    services.reader_sanitize) have their iframe/SVG embeds stripped; they only
    return when reader re-stores the entry on a content change. reader skips
    unchanged feeds via conditional GET, so we mark the feed stale first
    (reader's own mechanism to ignore the cached ETag/Last-Modified) and then
    update it: the now-unsanitized re-parse yields a different content hash for
    those old entries, so reader re-stores them with embeds intact. Read/star
    state is preserved (reader keys on entry id, not content)."""
    try:
        with get_reader() as reader:
            # set_feed_stale is reader's supported "ignore HTTP caching on next
            # update" flag (used by its own --new=False path); private attr but
            # stable across reader 3.x.
            reader._storage.set_feed_stale(feed_url, True)
            try:
                updated = reader.update_feed(feed_url)
            except Exception as exc:
                # If the host refused our honest UA, flag it for browser identity
                # and retry once — otherwise a WAF-blocked feed can never backfill.
                if FeedRefreshService._is_fetch_refusal(exc) and _flag_browser_ua_on_refusal(feed_url):
                    updated = reader.update_feed(feed_url)
                else:
                    raise
    except Exception as exc:  # FeedNotFoundError, network/parse errors
        LOGGER.warning("[reparse] failed for %s: %s", feed_url, exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    modified = int(getattr(updated, "modified", 0)) if updated else 0
    new = int(getattr(updated, "new", 0)) if updated else 0
    return JSONResponse({"ok": True, "modified": modified, "new": new})


@router.post("/feeds/move")
def move_feed(
    request: Request,
    feed_url: str = Form(...),
    from_folder_id: int = Form(...),
    to_folder_id: int = Form(...),
    current_folder_id: int | None = Form(default=None),
    current_list_feed_url: str | None = Form(default=None),
    sort_by: str | None = Form(default=None),
    sort_dir: str | None = Form(default=None),
    read_filter: str | None = Form(default=None),
    star_only: str | None = Form(default=None),
    resume_read_filter: str | None = Form(default=None),
):
    normalized_read_filter = normalize_read_filter(read_filter)
    sort_query = build_sort_query(sort_by, sort_dir)
    star_only_query = build_star_only_query(star_only)
    resume_read_filter_query = build_resume_read_filter_query(resume_read_filter, active_read_filter=normalized_read_filter)
    read_filter_query = build_read_filter_query(read_filter)

    # Only "follow" the feed to its new folder if it's the feed the user is
    # currently viewing. Right-clicking a feed you aren't looking at (to file it)
    # should leave your current view put.
    following = bool(current_list_feed_url) and current_list_feed_url == feed_url
    if following:
        dest_folder_id: int = to_folder_id
        dest_feed = feed_url
    else:
        dest_folder_id = current_folder_id if current_folder_id is not None else to_folder_id
        dest_feed = current_list_feed_url or ""

    def _dest(message: str) -> str:
        feed_q = f"&list_feed_url={quote_plus(dest_feed)}" if dest_feed else ""
        return (
            f"/?folder_id={dest_folder_id}{feed_q}"
            f"{sort_query}{read_filter_query}{star_only_query}{resume_read_filter_query}"
            f"&message={quote_plus(message)}"
        )

    def _respond(message: str, ok: bool = True):
        # AJAX caller (sidebar move submenu) wants JSON so it can relocate the
        # feed node in place instead of a full-page reload.
        requested_with = (request.headers.get("x-requested-with") or "").lower()
        if "lectio" in requested_with or requested_with == "xmlhttprequest":
            return JSONResponse(
                {
                    "ok": ok,
                    "message": message,
                    "following": following,
                    "feed_url": feed_url,
                    "from_folder_id": from_folder_id,
                    "to_folder_id": to_folder_id,
                },
                status_code=200 if ok else 500,
            )
        return RedirectResponse(url=_dest(message), status_code=303)

    if from_folder_id == to_folder_id:
        return _respond("Feed is already in that folder.")

    message = "Feed moved."
    ok = True
    try:
        move_feed_to_folder(feed_url, from_folder_id, to_folder_id)
    except ValueError:
        message = "Couldn't move the feed to that folder."
        ok = False
    except Exception:
        LOGGER.exception("[feeds/move] failed feed=%s -> folder=%s", feed_url, to_folder_id)
        message = "Feed move failed."
        ok = False

    return _respond(message, ok)


@router.post("/feeds/disable")
def disable_feed_route(request: Request, folder_id: int = Form(...), feed_url: str = Form(...)):
    disable_feed(feed_url)
    # AJAX caller (e.g. the Feeds settings tree) wants JSON so it can update the
    # DOM in place instead of navigating away and closing the settings modal.
    requested_with = request.headers.get("x-requested-with", "").lower()
    if "lectio" in requested_with or requested_with == "xmlhttprequest":
        return JSONResponse({"ok": True, "feed_url": feed_url}, status_code=200)
    return RedirectResponse(url=f"/?folder_id={folder_id}", status_code=303)


@router.post("/feeds/enable")
def enable_feed_route(request: Request, folder_id: int | None = Form(default=None), feed_url: str = Form(...)):
    enable_feed(feed_url)
    requested_with = request.headers.get("x-requested-with", "").lower()
    if "lectio" in requested_with or requested_with == "xmlhttprequest":
        return JSONResponse({"ok": True, "feed_url": feed_url}, status_code=200)
    dest = f"/?folder_id={folder_id}" if folder_id else "/"
    return RedirectResponse(url=dest, status_code=303)


@router.post("/feeds/toggle-updates")
def toggle_feed_updates(feed_url: str = Form(...), enabled: str = Form(...)):
    """Pause/resume = enable/disable a feed (same unified state). Delegates to
    enable_feed/disable_feed, which set both Lectio's disabled_feeds row and
    reader's updates_enabled flag so every surface agrees."""
    want_enabled = enabled.lower() in ("1", "true", "yes")
    try:
        if want_enabled:
            enable_feed(feed_url)
        else:
            disable_feed(feed_url)
        return JSONResponse({"ok": True, "updates_enabled": want_enabled})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.post("/feeds/change-url")
def change_feed_url_route(old_url: str = Form(...), new_url: str = Form(...), force: int = Form(0)):
    """Change the URL of a feed, migrating all associated data.

    Unless ``force`` is set, the new URL is validated like Add Feed: it's
    probed, and if it's a real feed (or a page that advertises exactly the
    feed we want) the resolved feed URL is used. A URL that doesn't validate
    returns needs_confirm so the UI can offer 'Change anyway' — feeds behind
    auth or bot-walls that Lectio can't fetch are still allowed on override."""
    new_url = assume_https_if_schemeless(new_url.strip())
    parsed = urlparse(new_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return JSONResponse({"ok": False, "error": "Invalid URL — must be http or https."}, status_code=400)
    if new_url == old_url:
        return JSONResponse({"ok": False, "error": "New URL is the same as the current URL."}, status_code=400)

    # Validate/resolve the target (skipped on force). Deliberately does NOT fall
    # back to a Page Feed — Change URL is for swapping one real feed for another.
    if not force:
        from services.feed_discovery import probe_url as _probe_url

        result = _probe_url(new_url)
        feeds = result.get("feeds") or []
        if result.get("status") in ("feed", "feeds") and feeds:
            resolved = str(feeds[0]["url"])  # resolved feed (post-redirect / discovered)
            # A PAGE that advertises a feed on another host is advertising a
            # different publication, not a redirect of what was typed: a section
            # page hands back the network-wide feed, and swapping it in silently
            # replaces the subscription with something far broader — then seeds a
            # host-alias rule for it. Ask first. A direct feed URL that redirects
            # across hosts is still the same feed, so that resolves silently.
            typed_host = _normalize_alias_host(parsed.netloc)
            resolved_host = _normalize_alias_host(urlparse(resolved).netloc)
            if not result.get("direct") and typed_host and resolved_host and typed_host != resolved_host:
                return JSONResponse(
                    {
                        "ok": False,
                        "needs_confirm": True,
                        "attempted_url": new_url,
                        "resolved_url": resolved,
                        "error": f"That page advertises a feed on a different site:\n\n{resolved}\n\n"
                        "It may cover much more than the page you pasted. Use it anyway?",
                    },
                    status_code=422,
                )
            new_url = resolved
        else:
            return JSONResponse(
                {
                    "ok": False,
                    "needs_confirm": True,
                    "error": (result.get("message") or "That URL doesn't look like a feed.") + " Change anyway?",
                    "attempted_url": new_url,
                },
                status_code=422,
            )
        if new_url == old_url:
            return JSONResponse({"ok": False, "error": "That resolves to the feed's current URL."}, status_code=400)
        # Name the URL that actually collides — reader raises FeedExistsError for
        # the resolved address, which the user never typed and cannot see.
        with get_reader() as reader:
            if reader.get_feed(new_url, None) is not None:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": f"That resolves to {new_url}, which you are already "
                        "subscribed to. Consolidate the duplicate instead (Settings → Feeds → Utilities).",
                    },
                    status_code=409,
                )

    try:
        with get_reader() as reader:
            reader.change_feed_url(old_url, new_url)
    except FeedNotFoundError:
        # The submitted "current" URL isn't in reader — almost always a stale
        # page whose feed was redirected out from under it (a FeedBurner-style
        # redirector resolving to the publisher's own URL). Point the user at
        # a reload rather than the raw reader exception.
        return JSONResponse(
            {
                "ok": False,
                "error": "This feed's current URL has changed (it may have been "
                "redirected). Reload the page and try again — the Change URL field will show "
                "the up-to-date URL.",
            },
            status_code=409,
        )
    except FeedExistsError:
        return JSONResponse(
            {
                "ok": False,
                "error": "A feed with that URL already exists. Consolidate the duplicate instead (Settings → Feeds → Utilities).",
            },
            status_code=409,
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    # Migrate all meta DB tables that reference the old feed_url.
    _feed_url_tables = [
        "archived_entry",
        "archived_asset_link",
        "folder_feeds",
        "saved_entries",
        "entry_read_state",
        "entry_unread_batch",
        "entry_unstar_batch",
        "read_history",
        "feed_failure_state",
        "entry_lead_images",
        "entry_feed_tags",
        "feed_lead_image_strategy",
        "disabled_feeds",
        "feed_display_prefs",
        "feed_strategy_cache",
        # Dismissed suggestion chips are per feed, so they have to follow the feed when its URL is
        # rewritten — otherwise the dismissals orphan and every chip the user waved off comes back.
        "suppressed_feed_tags",
        "suppressed_feed_attachment_exts",
        "rule_run_log_entries",
        "email_batch_queue",
    ]
    _was_needs_replacement = old_url in get_feeds_needing_replacement()
    with get_meta_connection() as conn:
        for table in _feed_url_tables:
            try:
                conn.execute(f"UPDATE {table} SET feed_url = ? WHERE feed_url = ?", (new_url, old_url))
            except Exception:
                pass
        # highlight_keywords uses scope/scope_id rather than feed_url
        try:
            conn.execute(
                "UPDATE highlight_keywords SET scope_id = ? WHERE scope = 'feed' AND scope_id = ?",
                (new_url, old_url),
            )
        except Exception:
            pass

    # Migrate starred archive DB tables.
    try:
        with archive_conn() as arch_conn:
            for table in ("archived_entry", "archived_asset_link"):
                try:
                    arch_conn.execute(f"UPDATE {table} SET feed_url = ? WHERE feed_url = ?", (new_url, old_url))
                except Exception:
                    pass
    except Exception:
        pass

    # Migrate lead-image in-memory caches: re-key (old_url, entry_id) → (new_url, entry_id).
    lead_image_service.rename_feed_url_in_cache(old_url, new_url)

    # Clear any backoff/failure state the old URL accumulated so the new URL
    # gets fetched immediately rather than waiting for the next scheduled retry.
    with get_meta_connection() as conn:
        conn.execute("DELETE FROM feed_failure_state WHERE feed_url = ?", (new_url,))
        # A URL change on a needs-replacement feed IS the replacement: clear the
        # dead-triage flag (migrated to new_url above) so it leaves the worklist.
        conn.execute("DELETE FROM feeds_needing_replacement WHERE feed_url IN (?, ?)", (old_url, new_url))
    if _was_needs_replacement:
        # It was disabled while dead; the working replacement should fetch again.
        enable_feed(new_url)

    # A feed that moved to a new HOST moved the site with it. Seed the same
    # alias rule Edit Website seeds, so the old domain keeps resolving: the
    # entry-link rebase, the dupe scan, the favicon, re-fetch AND the Website
    # shown in Feed Properties all read through feed_url_rewrites, so one rule
    # fixes all of them. Doing it by hand afterwards is easy to forget, and the
    # symptom (Website still naming a dead domain) does not look like something
    # the URL change caused.
    alias: dict | None = None
    _old_host = _normalize_alias_host(urlparse(old_url).netloc)
    _new_host = _normalize_alias_host(urlparse(new_url).netloc)
    if _old_host and _new_host and _old_host != _new_host:
        try:
            with get_meta_connection() as conn:
                already = conn.execute(
                    "SELECT 1 FROM feed_url_rewrites WHERE feed_url = ? AND from_host = ?",
                    (new_url, _old_host),
                ).fetchone()
                if already is None:
                    conn.execute(
                        "INSERT OR REPLACE INTO feed_url_rewrites (feed_url, from_host, to_host) VALUES (?, ?, ?)",
                        (new_url, _old_host, _new_host),
                    )
            if already is None:
                stats = migrate_feed_host_rewrite(new_url, {_old_host: _new_host})
                alias = {"from_host": _old_host, "to_host": _new_host, "migrated": int(stats.get("migrated", 0) or 0)}
        except Exception:  # noqa: BLE001 — the URL change itself already succeeded
            LOGGER.warning("[change-url] alias seeding failed for %s", new_url, exc_info=True)

    invalidate_meta_structure_cache()
    invalidate_problematic_feeds_cache()
    invalidate_unread_counts_cache()
    _bump_unread_counts_generation()

    # Bound to the requesting tenant: a bare Thread does not inherit
    # contextvars, so this refresh ran as the DEFAULT user and fetched into the
    # wrong (or no) database. The URL change itself succeeded, so the symptom
    # was a feed that had plainly moved yet still showed the OLD site's title
    # and link until some later scheduled cycle happened to pick it up —
    # looking for all the world like the change hadn't taken.
    threading.Thread(
        target=_run_in_user_context,
        args=(tenancy.current_user_id(), feed_refresh_service.update_feeds, [new_url]),
        daemon=True,
        name="refresh-after-url-change",
    ).start()

    # The folder the feed sits in, so the client can navigate back to it WITH
    # its scope. Without this the redirect carried only list_feed_url, and a
    # feed URL with no folder_id leaves the sidebar with nothing to select —
    # the feed is open in the list but invisible in the tree, so there is no
    # way back to its context menu.
    folder_id = None
    try:
        with get_meta_connection() as conn:
            row = conn.execute("SELECT folder_id FROM folder_feeds WHERE feed_url = ? LIMIT 1", (new_url,)).fetchone()
            folder_id = int(row["folder_id"]) if row else None
    except Exception:  # noqa: BLE001 — navigation nicety, never fail the change
        LOGGER.warning("[change-url] folder lookup failed for %s", new_url, exc_info=True)

    return JSONResponse(
        {
            "ok": True,
            "new_url": new_url,
            "folder_id": folder_id,
            "old_host": _old_host,
            "alias": alias,
        }
    )


@router.post("/feeds/unsubscribe")
def unsubscribe_feed(
    request: Request,
    folder_id: int = Form(...),
    feed_url: str = Form(...),
    sort_by: str | None = Form(default=None),
    sort_dir: str | None = Form(default=None),
    read_filter: str | None = Form(default=None),
    star_only: str | None = Form(default=None),
    resume_read_filter: str | None = Form(default=None),
    migrate_curation_to: str | None = Form(default=None),
    restar_curated: str = Form(default=""),
    drop_curation: str = Form(default=""),
):
    normalized_read_filter = normalize_read_filter(read_filter)
    sort_query_s = build_sort_query(sort_by, sort_dir)
    star_only_query = build_star_only_query(star_only)
    resume_read_filter_query = build_resume_read_filter_query(resume_read_filter, active_read_filter=normalized_read_filter)
    read_filter_query_s = build_read_filter_query(read_filter)

    ok = True
    message = "Feed unsubscribed."
    try:
        # Before anything is removed: the option to bring this feed's curated
        # items back to the top of the Inbox. Runs first so the entries are
        # still readable and so the capture it enqueues is flushed by the
        # force-archive both branches below already perform.
        restarred = restar_curated_entries(feed_url) if normalize_star_only(restar_curated) else 0

        # "Unsubscribe and drop everything": strip the keep signals BEFORE the
        # removal, so the purge below finds nothing worth preserving and the
        # posts do not survive in Saved as orphan archives.
        dropped: dict[str, int] | None = drop_all_curation(feed_url) if normalize_star_only(drop_curation) else None

        with get_meta_connection() as conn:
            conn.execute(
                "DELETE FROM folder_feeds WHERE folder_id = ? AND feed_url = ?",
                (folder_id, feed_url),
            )
            still_used = conn.execute(
                "SELECT 1 FROM folder_feeds WHERE feed_url = ? LIMIT 1",
                (feed_url,),
            ).fetchone()

        if not still_used:
            # When a target feed is given, migrate this feed's tags/stars onto it
            # (synthesizing entries) instead of archiving the stars — so curation
            # isn't lost on unsubscribe.
            _migrate_to = (migrate_curation_to or "").strip() or None
            if _migrate_to == feed_url:
                _migrate_to = None  # can't migrate onto itself
            with get_reader() as reader:
                with get_meta_connection() as conn:
                    purge_orphaned_feed(
                        reader,
                        conn,
                        feed_url,
                        archive_pending=_migrate_to is None,
                        migrate_curation_to=_migrate_to,
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO declined_feeds (feed_url, declined_at) VALUES (?, ?)",
                        (feed_url, datetime.now().isoformat()),
                    )
                    conn.commit()
        if dropped:
            message = (
                "Feed unsubscribed; "
                f"{dropped['untagged']} untagged, {dropped['unstarred']} unstarred, "
                f"{dropped['archives']} offline cop{'y' if dropped['archives'] == 1 else 'ies'} deleted."
            )
        if restarred:
            message += f" {restarred} curated post{'' if restarred == 1 else 's'} moved to the top of the Inbox."
        invalidate_meta_structure_cache()
    except Exception as exc:
        ok = False
        message = f"Unsubscribe failed: {exc}"

    # AJAX caller (e.g. problematic-feeds modal trash button) wants a JSON
    # response so it can update the DOM in place instead of navigating away.
    requested_with = request.headers.get("x-requested-with", "").lower()
    if "lectio" in requested_with or requested_with == "xmlhttprequest":
        return JSONResponse({"ok": ok, "feed_url": feed_url, "message": message}, status_code=200 if ok else 500)

    return RedirectResponse(
        url=(
            f"/?folder_id={folder_id}"
            f"{sort_query_s}"
            f"{read_filter_query_s}"
            f"{star_only_query}"
            f"{resume_read_filter_query}"
            f"&message={quote_plus(message)}"
        ),
        status_code=303,
    )


def feed_curation_counts(reader, conn: sqlite3.Connection, feed_url: str) -> dict:
    """Count the manual tags and stars a feed carries (curation that would be lost
    on unsubscribe). Returns ``{"tagged": n, "stars": n}`` — ``tagged`` is the number
    of entries with at least one manual tag, ``stars`` the number of saved entries."""
    stars = conn.execute("SELECT COUNT(*) FROM saved_entries WHERE feed_url = ?", (feed_url,)).fetchone()[0]
    tagged = 0
    try:
        for e in reader.get_entries(feed=feed_url):
            keys = [_extract_tag_key(t) for t in reader.get_tags(e.resource_id)]
            if any(k and k.startswith(MANUAL_TAG_KEY_PREFIX) for k in keys):
                tagged += 1
    except Exception:  # noqa: BLE001
        pass
    return {"tagged": tagged, "stars": int(stars)}


@router.get("/feeds/curation-count")
def feed_curation_count_route(feed_url: str = Query(...)):
    """Return how much curation (manual tags + stars) a feed carries, so the UI
    can offer to migrate it before an unsubscribe drops it."""
    try:
        with get_reader() as reader:
            with get_meta_connection() as conn:
                counts = feed_curation_counts(reader, conn, feed_url)
                # Candidate migration targets: every other subscribed feed, so the
                # dialog's picker doesn't depend on what's rendered in the DOM.
                # Prefer the user's custom title (what they see in the sidebar)
                # over reader's real feed title, and sort by that rendered title so
                # the picker order matches the labels (reader's sort="title" orders
                # by the real title, which can differ once user_title is applied).
                candidates = sorted(
                    (
                        {"url": f.url, "title": getattr(f, "user_title", None) or f.title or f.url}
                        for f in reader.get_feeds()
                        if f.url != feed_url
                    ),
                    key=lambda c: c["title"].lower(),
                )
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("[curation] count failed for %s: %s", feed_url, exc)
        return JSONResponse({"error": "Could not read this feed's curation."}, status_code=500)
    counts["candidates"] = candidates
    return JSONResponse(counts)
