"""Inoreader OAuth (connect/callback/disconnect) and the import routes.

The heavy lifting (the file-based import loop and the API-driven drip step)
lives in services/inoreader_import.py; this module is the thin route layer
plus the two import paths (JSON upload, direct JSON body) simple enough to
stay here.
"""

from __future__ import annotations

import io
import json
import secrets
import sqlite3
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse

from main import (
    LECTIO_PUBLIC_URL,
    MANUAL_TAG_KEY_PREFIX,
    SETTING_INOREADER_ACCESS_TOKEN,
    SETTING_INOREADER_EXPORT_DIR,
    SETTING_INOREADER_IMPORT_STATE,
    SETTING_INOREADER_OAUTH_STATE,
    SETTING_INOREADER_REFRESH_TOKEN,
    SETTING_INOREADER_TOKEN_EXPIRES_AT,
    _run_in_user_context,
    delete_setting,
    get_inoreader_credentials,
    get_meta_connection,
    get_reader,
    get_runtime_setting,
    get_setting,
    inoreader_connected,
    set_setting,
)
from services import inoreader as inoreader_service
from services import tenancy
from services.inoreader_import import _inoreader_drip_step, _inoreader_local_import_worker
from services.migration_common import _canonical_feed_url_lookup, _canonicalize_item_feed_urls, _resolve_feed_url

router = APIRouter()


def _inoreader_redirect_uri(request: Request) -> str:
    """The registered callback URL — must exactly match Inoreader developer console."""
    base = LECTIO_PUBLIC_URL
    if base:
        return f"{base}/inoreader/oauth/callback"
    return str(request.url_for("inoreader_oauth_callback"))


@router.get("/integrations/inoreader/oauth/connect")
def inoreader_oauth_connect(request: Request):
    """Kick off the Inoreader OAuth flow → redirect to Inoreader's consent page."""
    cid, secret = get_inoreader_credentials()
    if not cid or not secret:
        return RedirectResponse(
            url="/?message="
            + quote_plus(
                "Inoreader OAuth client is not configured (set INOREADER_CLIENT_ID/SECRET "
                "or enter them in Settings → Integrations → Inoreader)."
            ),
            status_code=303,
        )
    state = secrets.token_urlsafe(24)
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_INOREADER_OAUTH_STATE, state)
    url = inoreader_service.authorize_url(cid, _inoreader_redirect_uri(request), state)
    return RedirectResponse(url=url, status_code=303)


@router.get("/inoreader/oauth/callback", name="inoreader_oauth_callback")
def inoreader_oauth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    """OAuth redirect target registered at inoreader.com developer console.

    The exact path ``/inoreader/oauth/callback`` must match what is registered
    there — do not rename or move this route without updating the console.
    """
    if error:
        return RedirectResponse(
            url="/?message=" + quote_plus(f"Inoreader authorization failed: {error}"),
            status_code=303,
        )
    with get_meta_connection() as conn:
        expected = get_setting(conn, SETTING_INOREADER_OAUTH_STATE) or ""
    if not code or not state or state != expected:
        return RedirectResponse(
            url="/?message=" + quote_plus("Inoreader authorization failed (bad state)."),
            status_code=303,
        )
    cid, secret = get_inoreader_credentials()
    try:
        data = inoreader_service.exchange_code(cid, secret, code, _inoreader_redirect_uri(request))
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(
            url="/?message=" + quote_plus(f"Inoreader connect failed: {exc}"),
            status_code=303,
        )
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_INOREADER_ACCESS_TOKEN, data["access_token"])
        if data.get("refresh_token"):
            set_setting(conn, SETTING_INOREADER_REFRESH_TOKEN, data["refresh_token"])
        set_setting(conn, SETTING_INOREADER_TOKEN_EXPIRES_AT, str(time.time() + float(data.get("expires_in", 3600))))
        delete_setting(conn, SETTING_INOREADER_OAUTH_STATE)
    return RedirectResponse(
        url="/?message=" + quote_plus("Inoreader account connected. Go to Settings → Integrations → Inoreader to start the migration."),
        status_code=303,
    )


@router.post("/integrations/inoreader/oauth/disconnect")
def inoreader_oauth_disconnect():
    with get_meta_connection() as conn:
        for key in (
            SETTING_INOREADER_ACCESS_TOKEN,
            SETTING_INOREADER_REFRESH_TOKEN,
            SETTING_INOREADER_TOKEN_EXPIRES_AT,
            SETTING_INOREADER_OAUTH_STATE,
        ):
            delete_setting(conn, key)
    return JSONResponse({"ok": True})


@router.get("/integrations/inoreader/import/status")
def inoreader_import_status():
    """Return the current import checkpoint state as JSON."""
    with get_meta_connection() as conn:
        raw = get_setting(conn, SETTING_INOREADER_IMPORT_STATE) or ""
    if not raw:
        return JSONResponse({"phase": None, "done": False, "running": False})
    try:
        state = json.loads(raw)
    except Exception:
        return JSONResponse({"phase": "error", "done": False, "running": False})
    return JSONResponse({**state, "running": False})


@router.post("/integrations/inoreader/import/start")
def inoreader_import_start(delete_mode: int = Form(default=0), since: str = Form(default="")):
    """Initialise (or reinitialise) the API-driven import state and run the first drip step.

    ``since`` — optional ISO date (YYYY-MM-DD) or Unix timestamp string; only fetch
    items newer than this. Useful when a file import already covered older history.
    """
    if not inoreader_connected():
        return JSONResponse({"ok": False, "error": "Not connected"}, status_code=400)
    since_ot: int | None = None
    if since:
        try:
            since_ot = int(since)
        except ValueError:
            try:
                since_ot = int(datetime.fromisoformat(since.strip()).timestamp())
            except Exception:
                pass
    now = datetime.now(timezone.utc).isoformat()
    state = {
        "phase": "subscriptions",
        "subs_added": 0,
        "label_ids": [],
        "label_cursor": 0,
        "label_continuation": None,
        "starred_continuation": None,
        "items_tagged": 0,
        "items_starred": 0,
        "delete_mode": bool(delete_mode),
        "since_ot": since_ot,
        "z1_remaining": None,
        "error": None,
        "done": False,
        "started_at": now,
        "updated_at": now,
    }
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_INOREADER_IMPORT_STATE, json.dumps(state))
    # Run the first drip step immediately (best-effort, errors surface in status).
    _uid = tenancy.current_user_id()
    threading.Thread(
        target=lambda: _run_in_user_context(_uid, _inoreader_drip_step),
        daemon=True,
    ).start()
    return JSONResponse({"ok": True, "state": state})


@router.post("/integrations/inoreader/import/run")
def inoreader_import_run():
    """Manually trigger one drip step (for "Run now" button in UI)."""
    if not inoreader_connected():
        return JSONResponse({"ok": False, "error": "Not connected"}, status_code=400)
    _uid = tenancy.current_user_id()
    threading.Thread(
        target=lambda: _run_in_user_context(_uid, _inoreader_drip_step),
        daemon=True,
    ).start()
    return JSONResponse({"ok": True})


@router.post("/integrations/inoreader/import/reset")
def inoreader_import_reset():
    """Clear the import checkpoint so the migration can be restarted."""
    with get_meta_connection() as conn:
        delete_setting(conn, SETTING_INOREADER_IMPORT_STATE)
    return JSONResponse({"ok": True})


@router.post("/integrations/inoreader/import/local")
def inoreader_import_local():
    """Server-side import: scan the configured export directory for JSON files and
    import them all in background. Supports both InoreaderExportTool format (plain
    list) and native Inoreader export format (dict with 'items' key).

    The directory is set via Settings → Integrations → Inoreader (export dir field)
    or the INOREADER_EXPORT_DIR env var. It must contain .json files — typically one
    per label from ExportTool, plus starred-*.json from a native export ZIP.
    """
    export_dir = get_runtime_setting(SETTING_INOREADER_EXPORT_DIR, "").strip()
    if not export_dir:
        return JSONResponse({"ok": False, "error": "No export directory configured."}, status_code=400)
    dir_path = Path(export_dir)
    if not dir_path.is_dir():
        return JSONResponse({"ok": False, "error": f"Path is not a directory: {export_dir}"}, status_code=400)
    json_files = sorted(dir_path.glob("*.json"))
    if not json_files:
        return JSONResponse({"ok": False, "error": "No .json files found in that directory."}, status_code=400)

    now = datetime.now(timezone.utc).isoformat()
    state = {
        "phase": "local_files",
        "files_total": len(json_files),
        "files_done": 0,
        "current_file": "",
        "subs_added": 0,
        "items_tagged": 0,
        "items_starred": 0,
        "errors": 0,
        "done": False,
        "error": None,
        "started_at": now,
        "updated_at": now,
    }
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_INOREADER_IMPORT_STATE, json.dumps(state))

    _uid = tenancy.current_user_id()
    threading.Thread(
        target=lambda: _run_in_user_context(_uid, _inoreader_local_import_worker, json_files),
        daemon=True,
    ).start()
    return JSONResponse({"ok": True, "files": len(json_files), "state": state})


@router.post("/integrations/inoreader/import/upload")
async def inoreader_import_upload(files: list[UploadFile] = File(...)):
    """Browser-upload import: accept one or more .json files or a single .zip.

    Extracts all .json files to a server-side temp dir and starts the same
    background worker as the server-path import. Returns immediately; poll
    /integrations/inoreader/import/status for progress.
    """
    # Read all uploaded content first — this can take many seconds for large ZIPs.
    # Only touch the meta DB after all I/O is done to avoid lock timeouts.
    tmp_dir = Path(tempfile.mkdtemp(prefix="ino_import_"))
    json_paths: list[Path] = []

    for upload in files:
        name = upload.filename or ""
        content = await upload.read()
        if name.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(content)) as zf:
                    for member in zf.namelist():
                        if member.lower().endswith(".json") and not member.startswith("__MACOSX"):
                            out = tmp_dir / Path(member).name
                            out.write_bytes(zf.read(member))
                            json_paths.append(out)
            except zipfile.BadZipFile:
                return JSONResponse({"ok": False, "error": "Invalid or corrupt ZIP file."}, status_code=400)
        elif name.lower().endswith(".json"):
            out = tmp_dir / name
            out.write_bytes(content)
            json_paths.append(out)

    if not json_paths:
        return JSONResponse({"ok": False, "error": "No .json files found in upload."}, status_code=400)

    # All file I/O is done. Open a fresh short-lived connection with a generous
    # timeout — the thread-local pool connection can't be used safely here because
    # async coroutines on the same thread share it and may have it mid-transaction.
    json_paths = sorted(json_paths)
    now = datetime.now(timezone.utc).isoformat()
    uid = tenancy.current_user_id()
    _meta_conn = sqlite3.connect(str(tenancy.meta_db_path(uid)), timeout=30.0)
    _meta_conn.row_factory = sqlite3.Row
    _meta_conn.execute("PRAGMA journal_mode=WAL")
    _meta_conn.execute("PRAGMA busy_timeout=30000")
    try:
        raw = get_setting(_meta_conn, SETTING_INOREADER_IMPORT_STATE) or "{}"
        existing = json.loads(raw)
        if existing.get("phase") == "local_files" and not existing.get("done") and not existing.get("error"):
            return JSONResponse({"ok": False, "error": "An import is already running. Reset first."}, status_code=409)
        state = {
            "phase": "local_files",
            "files_total": len(json_paths),
            "files_done": 0,
            "current_file": "",
            "subs_added": 0,
            "items_tagged": 0,
            "items_starred": 0,
            "errors": 0,
            "done": False,
            "error": None,
            "started_at": now,
            "updated_at": now,
        }
        set_setting(_meta_conn, SETTING_INOREADER_IMPORT_STATE, json.dumps(state))
        _meta_conn.commit()
    finally:
        _meta_conn.close()

    _uid = tenancy.current_user_id()
    threading.Thread(
        target=lambda: _run_in_user_context(_uid, _inoreader_local_import_worker, json_paths, tmp_dir),
        daemon=True,
    ).start()
    return JSONResponse({"ok": True, "files": len(json_paths)})


@router.post("/integrations/inoreader/import/json")
async def inoreader_import_json(request: Request, file: UploadFile = File(...)):
    """Path B: import from an InoreaderExportTool JSON file (no API calls).

    Subscribes any unknown feeds and applies starred/label state to any entries
    that are already in the reader (best-effort — new feeds won't have entries
    until after their first fetch).
    """
    try:
        raw = await file.read()
        data = json.loads(raw)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"Invalid JSON: {exc}"}, status_code=400)

    items = inoreader_service.parse_export_json(data if isinstance(data, list) else data.get("items", []))

    # Canonicalize each incoming feed URL once so the subscribe loop and the
    # per-entry tag/star keying below share the same value — variants merge into
    # an existing subscription instead of creating duplicates.
    _canonicalize_item_feed_urls(items)

    feeds_added = 0
    items_starred = 0
    items_tagged = 0

    # Subscribe missing feeds
    feed_urls = {item["feed_url"] for item in items if item["feed_url"]}
    with get_reader() as reader:
        existing = _canonical_feed_url_lookup(reader)
        for furl in feed_urls:
            if furl not in existing:
                try:
                    reader.add_feed(furl, exist_ok=True)
                    existing[furl] = furl
                    feeds_added += 1
                except Exception:
                    pass

    # Apply stars and tags to entries already in reader
    with get_reader() as reader:
        existing = _canonical_feed_url_lookup(reader)
        with get_meta_connection() as conn:
            for item in items:
                entry_url = item["url"]
                # Resolve to whatever's actually stored in reader for this feed
                # (may predate canonicalization) so the lookup below finds the
                # real, already-existing entry instead of silently missing it.
                feed_url = _resolve_feed_url(item["feed_url"], existing)
                if not entry_url or not feed_url:
                    continue
                # Tag
                for label_name in item["labels"]:
                    if inoreader_service.label_is_tag(label_name):
                        tag_key = f"{MANUAL_TAG_KEY_PREFIX}{label_name.lower()}"
                        try:
                            entry = reader.get_entry((feed_url, entry_url), None)
                            if entry:
                                reader.set_tag(entry, tag_key)
                                items_tagged += 1
                        except Exception:
                            pass
                # Star
                if item["starred"]:
                    try:
                        entry = reader.get_entry((feed_url, entry_url), None)
                        if entry:
                            conn.execute(
                                "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id) VALUES (?, ?)",
                                (feed_url, entry_url),
                            )
                            items_starred += 1
                    except Exception:
                        pass

    return JSONResponse(
        {
            "ok": True,
            "feeds_added": feeds_added,
            "items_starred": items_starred,
            "items_tagged": items_tagged,
            "total_items": len(items),
        }
    )
