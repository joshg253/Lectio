"""Feed-management surface (Plan.md's main.py/index.html breakup, Stage 8 of
the route-by-URL-prefix split -- the biggest single cluster at 60 routes,
scoped into its own A-E sub-stages so it doesn't land as one huge diff).

**This file is not done yet.** Sub-stage A (folder CRUD + tree reads) is the
only part that exists so far -- sub-stages B-E (feed discovery/add flow,
display/thumbnail strategy config, network/fetch settings + lifecycle, and
tags/attachments/curation/bulk ops) each add more routes to this same module
in later tasks. Don't assume the 10 routes below are the final contents.

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
"""

from __future__ import annotations

from typing import cast
from urllib.parse import quote_plus

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from main import (
    _FOLDER_CADENCE_LAST_REFRESH_PREFIX,
    UNCATEGORIZED_FOLDER_ID,
    FeedInFolder,
    _bump_unread_counts_generation,
    _disambiguate_feed_titles,
    _mark_entries_as_read_for_view,
    build_read_filter_query,
    build_resume_read_filter_query,
    build_sort_query,
    build_star_only_query,
    delete_folder,
    get_all_feed_urls,
    get_all_reader_feed_urls,
    get_disabled_feed_urls,
    get_favicon_url,
    get_feed_title_map,
    get_folder_feed_urls,
    get_folder_properties,
    get_meta_connection,
    get_meta_structure_snapshot,
    get_problematic_feeds_cached,
    get_push_active_feed_urls,
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
    set_setting,
    sort_setting_keys,
    templates,
    unread_counts_cache,
    unread_counts_cache_lock,
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
