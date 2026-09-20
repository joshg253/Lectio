"""tt-rss migration: test connection / status / start / reset + the background worker."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from main import (
    LOGGER,
    SETTING_TTRSS_IMPORT_STATE,
    SETTING_TTRSS_PASSWORD,
    SETTING_TTRSS_URL,
    SETTING_TTRSS_USERNAME,
    _run_in_user_context,
    delete_setting,
    get_meta_connection,
    get_setting,
    set_setting,
)
from services import tenancy, url_guard
from services import ttrss as ttrss_service
from services.migration_common import _apply_migration_items

router = APIRouter()


@router.post("/integrations/ttrss/import/test")
async def ttrss_import_test(request: Request):
    """Test connection to a tt-rss instance. Body: {url, username, password} (empty fields fall back to stored settings)."""
    body = await request.json()
    with get_meta_connection() as conn:
        url = (body.get("url") or "").strip() or (get_setting(conn, SETTING_TTRSS_URL) or "")
        username = (body.get("username") or "").strip() or (get_setting(conn, SETTING_TTRSS_USERNAME) or "")
        password = (body.get("password") or "").strip() or (get_setting(conn, SETTING_TTRSS_PASSWORD) or "")
    if not url or not username or not password:
        return JSONResponse({"ok": False, "error": "url, username and password are required"}, status_code=400)
    try:
        info = ttrss_service.test_connection(url, username, password)
        return JSONResponse({"ok": True, **info})
    except ttrss_service.AuthError:
        return JSONResponse({"ok": False, "error": "Authentication failed — check your credentials."}, status_code=401)
    except url_guard.UnsafeURLError:
        return JSONResponse({"ok": False, "error": "That server URL is not allowed (use a public http(s) address)."}, status_code=400)
    except Exception:
        # Don't echo the raw exception to the client (CodeQL: info exposure); log it.
        LOGGER.warning("tt-rss connection test failed", exc_info=True)
        return JSONResponse({"ok": False, "error": "Could not connect to the tt-rss server."}, status_code=502)


@router.get("/integrations/ttrss/import/status")
def ttrss_import_status():
    with get_meta_connection() as conn:
        raw = get_setting(conn, SETTING_TTRSS_IMPORT_STATE) or ""
    if not raw:
        return JSONResponse({"state": None})
    try:
        return JSONResponse({"state": json.loads(raw)})
    except Exception:
        return JSONResponse({"state": None})


@router.post("/integrations/ttrss/import/start")
def ttrss_import_start():
    with get_meta_connection() as conn:
        url = get_setting(conn, SETTING_TTRSS_URL) or ""
        username = get_setting(conn, SETTING_TTRSS_USERNAME) or ""
        password = get_setting(conn, SETTING_TTRSS_PASSWORD) or ""
    if not url or not username or not password:
        return JSONResponse({"ok": False, "error": "tt-rss URL, username and password not configured"}, status_code=400)
    now = datetime.now(timezone.utc).isoformat()
    state = {
        "phase": "running",
        "subs_added": 0,
        "items_starred": 0,
        "items_tagged": 0,
        "errors": 0,
        "done": False,
        "error": None,
        "started_at": now,
        "updated_at": now,
    }
    _uid = tenancy.current_user_id()
    threading.Thread(
        target=lambda: _run_in_user_context(_uid, _ttrss_import_worker),
        daemon=True,
    ).start()
    return JSONResponse({"ok": True, "state": state})


@router.post("/integrations/ttrss/import/reset")
def ttrss_import_reset():
    with get_meta_connection() as conn:
        delete_setting(conn, SETTING_TTRSS_IMPORT_STATE)
    return JSONResponse({"ok": True})


def _ttrss_import_worker() -> None:
    with get_meta_connection() as conn:
        url = get_setting(conn, SETTING_TTRSS_URL) or ""
        username = get_setting(conn, SETTING_TTRSS_USERNAME) or ""
        password = get_setting(conn, SETTING_TTRSS_PASSWORD) or ""

    now = datetime.now(timezone.utc).isoformat()
    state: dict = {
        "phase": "running",
        "subs_added": 0,
        "items_starred": 0,
        "items_tagged": 0,
        "errors": 0,
        "done": False,
        "error": None,
        "started_at": now,
        "updated_at": now,
    }

    def _save():
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        with get_meta_connection() as c:
            set_setting(c, SETTING_TTRSS_IMPORT_STATE, json.dumps(state))

    _save()

    try:
        sid = ttrss_service.login(url, username, password)

        # Build category (folder) map.
        cats = ttrss_service.get_categories(url, sid)
        cat_name_map: dict[int, str] = {c["id"]: c["title"] for c in cats if c.get("title")}

        # Subscribe all feeds and assign folders.
        feeds = ttrss_service.get_all_feeds(url, sid)
        feed_info_map: dict[int, dict] = {}
        sub_items = []
        for idx, f in enumerate(feeds):
            feed_info_map[idx] = f  # placeholder; actual feed_id from headlines
            folder = cat_name_map.get(f.get("cat_id", 0), "")
            sub_items.append(
                {
                    "url": "",
                    "title": f.get("title", ""),
                    "published": None,
                    "feed_url": f.get("feed_url", ""),
                    "feed_title": f.get("title", ""),
                    "content": "",
                    "starred": False,
                    "tags": [],
                    "folder": folder,
                }
            )
        # Build a real feed_url → cat_id map for headline normalisation.
        url_to_cat: dict[str, int] = {f["feed_url"]: f.get("cat_id", 0) for f in feeds}
        # tt-rss headlines carry feed_id (int), not feed_url; build id→info map via a
        # second pass once we fetch headlines (which include feed_url in their data).
        _apply_migration_items(sub_items, state, _save)

        # Page through all starred headlines.
        limit = 200
        skip = 0
        while True:
            headlines = ttrss_service.get_starred_headlines(url, sid, limit=limit, skip=skip)
            if not headlines:
                break
            # Build feed_info_map from actual headline data (each headline has feed_url
            # and feed_title fields that appear in some tt-rss versions).
            feed_info_from_hl: dict[int, dict] = {}
            for hl in headlines:
                fid = hl.get("feed_id")
                if isinstance(fid, int) and fid not in feed_info_from_hl:
                    # Try to find the feed_url from the sub list or the headline itself.
                    hl_feed_url = hl.get("feed_url", "")
                    hl_feed_title = hl.get("feed_title", "")
                    cat_id = url_to_cat.get(hl_feed_url, 0)
                    feed_info_from_hl[fid] = {
                        "feed_url": hl_feed_url,
                        "title": hl_feed_title,
                        "cat_id": cat_id,
                    }
            items = [ttrss_service.normalize_headline(hl, feed_info_from_hl, cat_name_map) for hl in headlines]
            _apply_migration_items(items, state, _save)
            if len(headlines) < limit:
                break
            skip += limit

        state["phase"] = "done"
        state["done"] = True
        _save()
        LOGGER.info(
            "[ttrss-import] done: %d subs, %d starred, %d tagged, %d errors",
            state.get("subs_added", 0),
            state.get("items_starred", 0),
            state.get("items_tagged", 0),
            state.get("errors", 0),
        )
    except ttrss_service.AuthError as exc:
        state["error"] = str(exc)
        state["done"] = False
        _save()
    except Exception as exc:
        state["error"] = str(exc)[:300]
        state["done"] = False
        _save()
        LOGGER.exception("[ttrss-import] worker error")
