"""Tag management surface: `GET /tags/aliases`, `GET /tags/inventory`,
`POST /tags/aliases/preview`, `POST /tags/aliases/create`,
`POST /tags/aliases/delete`, `POST /feed-tags/dismiss`,
`GET /tags/global-suppressed`, `POST /tags/global-suppressed/add`,
`POST /tags/global-suppressed/remove`, `POST /tags/delete`, `POST /tags/rename`.

Stage 3 of the main.py route-by-URL-prefix split (Plan.md). `/feed-tags/dismiss`
is grouped here on purpose even though its URL doesn't start with `/tags` — it's
a tags concern (per-feed suggestion-chip suppression). `/entries/feed-tags`
(late chip delivery for the entry pane) is a different, entries-owned route and
stays in main.py.

normalize_tag_value and the alias/rename/delete helpers (list_tag_aliases,
tag_alias_preview, create_tag_alias, delete_tag_alias,
delete_manual_tag_everywhere, rename_manual_tag_everywhere,
normalize_tag_value_raw) stay in main.py: normalize_tag_value alone is on
every tag path there is (51 call sites per Plan.md's Landmines note), and the
rest are exercised directly (as main.<name>) by tests/integration/
test_tag_aliases.py, test_tag_removal.py, and test_orphan_entry_tags.py.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from main import (
    LOGGER,
    MANUAL_TAG_KEY_PREFIX,
    create_tag_alias,
    delete_manual_tag_everywhere,
    delete_tag_alias,
    feed_tag_service,
    get_meta_connection,
    get_reader,
    list_tag_aliases,
    normalize_tag_value,
    normalize_tag_value_raw,
    quote_plus,
    rename_manual_tag_everywhere,
    tag_alias_preview,
    tenancy,
)

router = APIRouter()


@router.get("/tags/aliases")
def list_tag_aliases_route():
    """Aliases plus the tag inventory behind the Settings -> Tags tab."""
    return JSONResponse({"ok": True, "aliases": list_tag_aliases()})


@router.get("/tags/inventory")
def tag_inventory_route(q: str = "", limit: int = 200, mine_only: int = 0):
    """Tags with counts, for spotting a spelling worth folding.

    Feed-provided and manual counts stay separate: they live in different stores
    (entry_feed_tags rows vs reader entry tags) and an alias rewrite touches
    both, so one merged number would hide which half is which — and did, when
    "33,520 tags" turned out to be 89 tags Josh applies plus 33,505 publisher
    names, 20,974 of them seen exactly once.

    **The full vocabulary is never materialized.** Filtering and the limit are
    pushed into SQL, so a search reads only matching rows and an unfiltered view
    reads only the top N by use. Building all 33k in Python and slicing to 200
    did the same work on every keystroke.

    Manual tags (89) are always loaded whole: they are the ones worth aliasing,
    they are cheap, and they must never be crowded out of the list by publisher
    tags with bigger counts.
    """
    needle = (q or "").strip().lower()
    limit = max(1, min(int(limit or 200), 500))
    like = f"%{needle}%"

    manual_counts: dict[str, int] = {}
    try:
        conn = sqlite3.connect(str(tenancy.reader_db_path()), timeout=5.0)
        try:
            sql = "SELECT key, COUNT(*) FROM entry_tags WHERE key LIKE ?" + (" AND LOWER(key) LIKE ?" if needle else "") + " GROUP BY key"
            args: list = [f"{MANUAL_TAG_KEY_PREFIX}%"] + ([like] if needle else [])
            for key, count in conn.execute(sql, args):
                short = str(key)[len(MANUAL_TAG_KEY_PREFIX) :]
                if short:
                    manual_counts[short] = manual_counts.get(short, 0) + int(count)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — the inventory is a convenience, never fatal
        LOGGER.debug("manual tag inventory unavailable", exc_info=True)

    feed_counts: dict[str, int] = {}
    feed_total = feed_once = 0
    with get_meta_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(CASE WHEN c = 1 THEN 1 ELSE 0 END), 0) AS once "
            "FROM (SELECT tag, COUNT(*) AS c FROM entry_feed_tags GROUP BY tag)"
        ).fetchone()
        if row:
            feed_total, feed_once = int(row["n"]), int(row["once"])
        if not mine_only:
            sql = "SELECT tag, COUNT(*) AS n FROM entry_feed_tags"
            args = []
            if needle:
                sql += " WHERE LOWER(tag) LIKE ?"
                args.append(like)
            # Over-fetch a little: several raw tags can normalize to one name,
            # so the top `limit` rows here can collapse to fewer entries.
            sql += " GROUP BY tag ORDER BY n DESC LIMIT ?"
            args.append(limit * 3)
            for r in conn.execute(sql, args):
                key = normalize_tag_value_raw(str(r["tag"])) or str(r["tag"])
                feed_counts[key] = feed_counts.get(key, 0) + int(r["n"])

    aliases = {a["alias"]: a["canonical"] for a in list_tag_aliases()}
    names = [n for n in set(manual_counts) | set(aliases) | set(feed_counts) if not needle or needle in n]
    # Yours first: a publisher tag with a big count is not more interesting than
    # one you actually file with.
    names.sort(key=lambda name: (-manual_counts.get(name, 0), -feed_counts.get(name, 0), name))
    items = [
        {
            "tag": name,
            "manual": manual_counts.get(name, 0),
            "feed": feed_counts.get(name, 0),
            "alias_of": aliases.get(name),
        }
        for name in names
    ]
    return JSONResponse(
        {
            "ok": True,
            "items": items[:limit],
            "scope": "mine" if mine_only else "all",
            "summary": {
                "manual": len(manual_counts),
                "feed": feed_total,
                "feed_seen_once": feed_once,
                "matched": len(items),
                "shown": min(len(items), limit),
                "truncated": len(items) > limit,
            },
        }
    )


@router.post("/tags/aliases/preview")
def preview_tag_alias_route(alias: str = Form(...), canonical: str = Form(...)):
    """Counts only — nothing is written, so the UI can show what would move."""
    return JSONResponse({"ok": True, **tag_alias_preview(alias, canonical)})


@router.post("/tags/aliases/create")
def create_tag_alias_route(alias: str = Form(...), canonical: str = Form(...), rewrite: int = Form(1)):
    result = create_tag_alias(alias, canonical, rewrite=bool(rewrite))
    if result.get("error"):
        return JSONResponse({"ok": False, **result}, status_code=400)
    return JSONResponse({"ok": True, **result})


@router.post("/tags/aliases/delete")
def delete_tag_alias_route(alias: str = Form(...)):
    """Drop the alias. Tags already folded stay folded — the rewrite happened,
    and un-folding would need to know which entries were moved, which is not
    recorded (deliberately: it is a rename, not a filter)."""
    return JSONResponse({"ok": True, "deleted": delete_tag_alias(alias)})


@router.post("/feed-tags/dismiss")
def dismiss_feed_tag(
    request: Request,
    feed_url: str = Form(...),
    tag: str = Form(...),
    dismissed: int = Form(default=1),
):
    """Hide (or restore) one feed-tag suggestion chip for one feed.

    The replacement for two failed heuristics. Automatic suppression hid tags the
    user wanted — "Lessons" on a guitar-lesson feed reads as boilerplate to every
    frequency- or name-based rule, yet it is the correct filing tag. The judgment
    is semantic, so it belongs to the person filing.

    Per feed, not global: "Forum" is noise on Slickdeals and might be a real topic
    elsewhere. The stored rows in `entry_feed_tags` are untouched either way —
    this hides a chip, it does not forget a fact.
    """
    feed_tag_service.set_tag_suppressed(feed_url, tag, bool(dismissed))
    return JSONResponse(
        {
            "ok": True,
            "feed_url": feed_url,
            "tag": tag,
            "dismissed": bool(dismissed),
            "suppressed": feed_tag_service.suppressed_tag_list(feed_url),
        }
    )


@router.get("/tags/global-suppressed")
def list_globally_suppressed_tags_route():
    """Tag values that never render as a suggestion chip on any feed, behind
    Settings -> Tags."""
    return JSONResponse({"ok": True, "tags": feed_tag_service.global_suppressed_tag_list()})


@router.post("/tags/global-suppressed/add")
def add_globally_suppressed_tag_route(tag: str = Form(...)):
    clean = (tag or "").strip()
    if not clean:
        return JSONResponse({"ok": False, "error": "Enter a tag."}, status_code=400)
    feed_tag_service.set_tag_globally_suppressed(clean, True)
    return JSONResponse({"ok": True, "tags": feed_tag_service.global_suppressed_tag_list()})


@router.post("/tags/global-suppressed/remove")
def remove_globally_suppressed_tag_route(tag: str = Form(...)):
    feed_tag_service.set_tag_globally_suppressed(tag, False)
    return JSONResponse({"ok": True, "tags": feed_tag_service.global_suppressed_tag_list()})


@router.post("/tags/delete")
def delete_manual_tag(
    request: Request,
    tag: str = Form(...),
):
    normalized = normalize_tag_value(tag)
    if not normalized:
        if request.headers.get("X-Requested-With") in ("lectio-ajax", "lectio-sidebar"):
            return JSONResponse({"ok": False, "error": "Invalid tag."}, status_code=400)
        return RedirectResponse(url="/", status_code=303)

    removed = delete_manual_tag_everywhere(normalized)

    if request.headers.get("X-Requested-With") in ("lectio-ajax", "lectio-sidebar"):
        return JSONResponse({"ok": True, "tag": normalized, "removed": removed})

    message = f"Removed #{normalized} from {removed} post{'' if removed == 1 else 's'}."
    return RedirectResponse(url=f"/?message={quote_plus(message)}", status_code=303)


@router.post("/tags/rename")
def rename_manual_tag(
    request: Request,
    old_tag: str = Form(...),
    new_tag: str = Form(...),
    force: str = Form(default=""),
):
    old_norm = normalize_tag_value(old_tag)
    new_norm = normalize_tag_value(new_tag)
    is_ajax = request.headers.get("X-Requested-With") in ("lectio-ajax", "lectio-sidebar")

    if not old_norm or not new_norm:
        if is_ajax:
            return JSONResponse({"ok": False, "error": "Invalid tag name."}, status_code=400)
        return RedirectResponse(url="/", status_code=303)
    if old_norm == new_norm:
        if is_ajax:
            return JSONResponse({"ok": False, "error": "New name is the same as the old name."}, status_code=400)
        return RedirectResponse(url="/", status_code=303)

    # Without force, warn the caller if new_tag already exists so the UI can
    # ask for explicit confirmation before merging two tags.
    if not force:
        with get_reader() as reader:
            new_key = f"{MANUAL_TAG_KEY_PREFIX}{new_norm}"
            if reader.get_entry_counts(tags=[new_key]).total > 0:
                if is_ajax:
                    return JSONResponse({"ok": False, "exists": True, "new_tag": new_norm})
                return RedirectResponse(url="/", status_code=303)

    count, merged = rename_manual_tag_everywhere(old_norm, new_norm)
    if is_ajax:
        return JSONResponse({"ok": True, "old_tag": old_norm, "new_tag": new_norm, "count": count, "merged": merged})
    return RedirectResponse(url="/", status_code=303)
