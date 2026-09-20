"""FreshRSS migration: test connection / status / start / reset + the background worker."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from main import (
    LOGGER,
    SETTING_FRESHRSS_IMPORT_STATE,
    SETTING_FRESHRSS_PASSWORD,
    SETTING_FRESHRSS_URL,
    SETTING_FRESHRSS_USERNAME,
    _run_in_user_context,
    delete_setting,
    get_meta_connection,
    get_setting,
    set_setting,
)
from services import freshrss as freshrss_service
from services import tenancy, url_guard
from services.migration_common import _apply_migration_items

router = APIRouter()


@router.post("/integrations/freshrss/import/test")
async def freshrss_import_test(request: Request):
    """Test connection to a FreshRSS instance. Body: {url, username, password} (empty fields fall back to stored settings)."""
    body = await request.json()
    with get_meta_connection() as conn:
        url = (body.get("url") or "").strip() or (get_setting(conn, SETTING_FRESHRSS_URL) or "")
        username = (body.get("username") or "").strip() or (get_setting(conn, SETTING_FRESHRSS_USERNAME) or "")
        password = (body.get("password") or "").strip() or (get_setting(conn, SETTING_FRESHRSS_PASSWORD) or "")
    if not url or not username or not password:
        return JSONResponse({"ok": False, "error": "url, username and password are required"}, status_code=400)
    try:
        info = freshrss_service.test_connection(url, username, password)
        return JSONResponse({"ok": True, **info})
    except freshrss_service.AuthError:
        return JSONResponse({"ok": False, "error": "Authentication failed — check your credentials."}, status_code=401)
    except url_guard.UnsafeURLError:
        return JSONResponse({"ok": False, "error": "That server URL is not allowed (use a public http(s) address)."}, status_code=400)
    except Exception:
        # Don't echo the raw exception to the client (CodeQL: info exposure); log it.
        LOGGER.warning("FreshRSS connection test failed", exc_info=True)
        return JSONResponse({"ok": False, "error": "Could not connect to the FreshRSS server."}, status_code=502)


@router.get("/integrations/freshrss/import/status")
def freshrss_import_status():
    with get_meta_connection() as conn:
        raw = get_setting(conn, SETTING_FRESHRSS_IMPORT_STATE) or ""
    if not raw:
        return JSONResponse({"state": None})
    try:
        return JSONResponse({"state": json.loads(raw)})
    except Exception:
        return JSONResponse({"state": None})


@router.post("/integrations/freshrss/import/start")
def freshrss_import_start():
    with get_meta_connection() as conn:
        url = get_setting(conn, SETTING_FRESHRSS_URL) or ""
        username = get_setting(conn, SETTING_FRESHRSS_USERNAME) or ""
        password = get_setting(conn, SETTING_FRESHRSS_PASSWORD) or ""
    if not url or not username or not password:
        return JSONResponse({"ok": False, "error": "FreshRSS URL, username and password not configured"}, status_code=400)
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
        target=lambda: _run_in_user_context(_uid, _freshrss_import_worker),
        daemon=True,
    ).start()
    return JSONResponse({"ok": True, "state": state})


@router.post("/integrations/freshrss/import/reset")
def freshrss_import_reset():
    with get_meta_connection() as conn:
        delete_setting(conn, SETTING_FRESHRSS_IMPORT_STATE)
    return JSONResponse({"ok": True})


def _freshrss_import_worker() -> None:
    with get_meta_connection() as conn:
        url = get_setting(conn, SETTING_FRESHRSS_URL) or ""
        username = get_setting(conn, SETTING_FRESHRSS_USERNAME) or ""
        password = get_setting(conn, SETTING_FRESHRSS_PASSWORD) or ""

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
            set_setting(c, SETTING_FRESHRSS_IMPORT_STATE, json.dumps(state))

    _save()

    try:
        token = freshrss_service.login(url, username, password)

        # Phase 1: subscriptions + folders.
        subs = freshrss_service.get_subscriptions(url, token)
        sub_items = []
        for sub in subs:
            feed_url = sub.get("feed_url", "")
            cats = sub.get("categories") or []
            folder = ""
            for cat in cats:
                label = cat.get("label") or ""
                if label and not freshrss_service.label_is_tag(label):
                    folder = label
                    break
            sub_items.append(
                {
                    "url": "",
                    "title": sub.get("title", ""),
                    "published": None,
                    "feed_url": feed_url,
                    "feed_title": sub.get("title", ""),
                    "content": "",
                    "starred": False,
                    "tags": [],
                    "folder": folder,
                }
            )
        _apply_migration_items(sub_items, state, _save)

        # Phase 2: labels → tags (page through each label stream).
        tags = freshrss_service.get_tags(url, token)
        label_names = [
            name for t in tags if (name := freshrss_service.label_name_from_tag_id(t.get("id", ""))) and freshrss_service.label_is_tag(name)
        ]
        for label_name in label_names:
            stream_id = freshrss_service.label_stream_id(label_name)
            continuation = None
            while True:
                items_raw, continuation = freshrss_service.get_stream_contents(url, token, stream_id, continuation=continuation, n=100)
                items = [freshrss_service.normalize_item(i) for i in items_raw]
                for item in items:
                    item["tags"] = [label_name]
                _apply_migration_items(items, state, _save)
                if not continuation:
                    break

        # Phase 3: starred entries.
        continuation = None
        while True:
            items_raw, continuation = freshrss_service.get_stream_contents(
                url, token, freshrss_service.STARRED_STREAM_ID, continuation=continuation, n=100
            )
            items = [freshrss_service.normalize_item(i) for i in items_raw]
            for item in items:
                item["starred"] = True
            _apply_migration_items(items, state, _save)
            if not continuation:
                break

        state["phase"] = "done"
        state["done"] = True
        _save()
        LOGGER.info(
            "[freshrss-import] done: %d subs, %d starred, %d tagged, %d errors",
            state.get("subs_added", 0),
            state.get("items_starred", 0),
            state.get("items_tagged", 0),
            state.get("errors", 0),
        )
    except freshrss_service.AuthError as exc:
        state["error"] = str(exc)
        state["done"] = False
        _save()
    except Exception as exc:
        state["error"] = str(exc)[:300]
        state["done"] = False
        _save()
        LOGGER.exception("[freshrss-import] worker error")
