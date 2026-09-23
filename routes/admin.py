"""Account self-service, admin user management, and debug/maintenance toggles:
`/account/*` (own password/API-token/username), `/admin/users/*` (admin-only
user management: create/disable/delete/reset-password/rename/vacuum of any
user -- only disable and delete reject targeting your own account;
rename/reset-password don't), `/admin/logs` (Admin -> Logs tab), and
`/debug/*` (lead-image cache clearing and feed-bypass toggles).

Stage 5 of the main.py route-by-URL-prefix split (Plan.md). No ordering
constraint: none of these handlers reference `_run_automation_after_refresh`
or anything else from the late `services.automation_rules` import, so this
module is imported alongside the plain `routes.compat_*`/`routes.tags`-style
modules rather than after that block.

`_username_error`/`_password_error`/`_account_redirect` (plus the
USERNAME_MIN_LEN/PASSWORD_MIN_LEN constants) and `_purge_thumb_cache_for_urls`
moved here with their routes -- each had no caller left in main.py once its
route(s) moved. `_dir_bytes` stayed in main.py despite `admin_vacuum_user`
being its only caller here: `routes/system.py` (Stage 1) also calls it for
the Admin -> storage-usage stats, so it's genuinely shared. `_is_web_admin`
and `_current_web_user` stay for the same reason (called from several
still-in-main.py routes). `_read_log_tail`/`_log_line_dt`/`_parse_local_ts`
(plus the `_LOG_LEVEL_RANK`/`_LOG_LINE_RE` constants they use) stay in
main.py and are imported back rather than moving with `admin_logs`:
`tests/unit/test_admin_log_tail.py` exercises them directly as
`main._read_log_tail` etc., the same "exercised directly by a dedicated test
file" reason Stage 3 used to keep `get_highlight_keywords` and friends in
main.py. `provision_user_storage` stays too -- also called from
`bootstrap_admin` -- and `delete_user_storage`, its lifecycle-pair sibling
defined right next to it far from this route cluster, stays alongside it
rather than being split out for a single caller.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from pathlib import Path
from urllib.parse import quote_plus, urlencode

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from main import (
    _THUMB_COVER_POS,
    _THUMB_H,
    _THUMB_W,
    DEBUG_MODE,
    LOGGER,
    PASSWORD_HASH_SCHEME,
    THUMB_CACHE_DIR,
    _current_web_user,
    _dir_bytes,
    _is_web_admin,
    _parse_local_ts,
    _read_log_tail,
    _run_in_user_context,
    delete_user_storage,
    get_reader,
    get_thumb_connection,
    lead_image_service,
    provision_user_storage,
    starred_archive_service,
    tenancy,
    user_store,
)
from services.users import UserExistsError

router = APIRouter()


# --- Account / user management ---

USERNAME_MIN_LEN, USERNAME_MAX_LEN = 4, 10
PASSWORD_MIN_LEN, PASSWORD_MAX_LEN = 6, 36


def _username_error(name: str) -> str | None:
    if not (USERNAME_MIN_LEN <= len(name) <= USERNAME_MAX_LEN):
        return f"Username must be {USERNAME_MIN_LEN}–{USERNAME_MAX_LEN} characters."
    if not tenancy.is_valid_user_id(name):
        return "Username may use only letters, digits, _ and -."
    return None


def _password_error(pw: str) -> str | None:
    if not (PASSWORD_MIN_LEN <= len(pw) <= PASSWORD_MAX_LEN):
        return f"Password must be {PASSWORD_MIN_LEN}–{PASSWORD_MAX_LEN} characters."
    return None


def _account_redirect(*, msg: str | None = None, error: str | None = None) -> RedirectResponse:
    params: dict[str, str] = {}
    if msg:
        params["msg"] = msg
    if error:
        params["error"] = error
    url = "/administration" + ("?" + urlencode(params) if params else "")
    return RedirectResponse(url=url, status_code=303)


@router.post("/account/password")
async def account_change_password(request: Request):
    if user_store is None:
        return Response(status_code=404)
    uid = _current_web_user(request)
    if not uid:
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    current = str(form.get("current_password") or "")
    new = str(form.get("new_password") or "")
    confirm = str(form.get("confirm_password") or "")
    row = user_store.get_by_id(uid)
    if row is None:
        return RedirectResponse(url="/login", status_code=303)
    # verify_login takes the (typed) username; we have the user_id from session.
    if user_store.verify_login(row["username"], current, default_scheme=PASSWORD_HASH_SCHEME) != uid:
        return RedirectResponse(url="/?message=" + quote_plus("Current password is incorrect."), status_code=303)
    if not new or new != confirm:
        return RedirectResponse(url="/?message=" + quote_plus("New password and confirmation do not match."), status_code=303)
    perr = _password_error(new)
    if perr:
        return RedirectResponse(url="/?message=" + quote_plus(perr), status_code=303)
    user_store.set_password(uid, new, scheme=PASSWORD_HASH_SCHEME)
    return RedirectResponse(url="/?message=" + quote_plus("Password changed."), status_code=303)


@router.post("/account/api-token/regenerate")
async def account_regenerate_token(request: Request):
    if user_store is None:
        return Response(status_code=404)
    uid = _current_web_user(request)
    if not uid:
        return RedirectResponse(url="/login", status_code=303)
    user_store.regenerate_api_token(uid)
    return RedirectResponse(
        url="/?message=" + quote_plus("API token regenerated — update your RSS clients."),
        status_code=303,
    )


@router.post("/account/username")
async def account_change_username(request: Request):
    """Self-service username change. Identity (user_id) is unchanged, so the
    session stays valid and data/tokens are unaffected."""
    if user_store is None:
        return Response(status_code=404)
    uid = _current_web_user(request)
    if not uid:
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    new_username = str(form.get("new_username") or "").strip()
    uerr = _username_error(new_username)
    if uerr:
        return RedirectResponse(url="/?message=" + quote_plus(uerr), status_code=303)
    try:
        user_store.rename_user(uid, new_username)
    except UserExistsError:
        return RedirectResponse(url="/?message=" + quote_plus(f"Username {new_username!r} is taken."), status_code=303)
    except ValueError:
        return RedirectResponse(url="/?message=" + quote_plus("Invalid username."), status_code=303)
    return RedirectResponse(url="/?message=" + quote_plus(f"Username changed to {new_username!r}."), status_code=303)


@router.post("/admin/users/create")
async def admin_create_user(request: Request):
    if user_store is None:
        return Response(status_code=404)
    admin = _current_web_user(request)
    if not _is_web_admin(admin):
        return Response(status_code=403)
    form = await request.form()
    username = str(form.get("username") or "").strip()
    password = str(form.get("password") or "")
    is_admin = bool(form.get("is_admin"))
    uerr = _username_error(username)
    if uerr:
        return _account_redirect(error=uerr)
    perr = _password_error(password)
    if perr:
        return _account_redirect(error=perr)
    try:
        new_user_id = user_store.create(username, password, is_admin=is_admin, scheme=PASSWORD_HASH_SCHEME)
        provision_user_storage(new_user_id)
    except UserExistsError:
        return _account_redirect(error=f"User {username!r} already exists.")
    except Exception:
        LOGGER.exception("admin create user failed")
        return _account_redirect(error="Could not create user (see server logs).")
    return _account_redirect(msg=f"Created user {username!r}.")


@router.post("/admin/users/disable")
async def admin_disable_user(request: Request):
    if user_store is None:
        return Response(status_code=404)
    admin = _current_web_user(request)
    if not _is_web_admin(admin):
        return Response(status_code=403)
    form = await request.form()
    target_id = str(form.get("user_id") or "")
    disabled = str(form.get("disabled") or "0") == "1"
    if target_id == admin and disabled:
        return _account_redirect(error="You cannot disable your own account.")
    target = user_store.get_by_id(target_id)
    if target is None:
        return _account_redirect(error="No such user.")
    user_store.set_disabled(target_id, disabled)
    return _account_redirect(msg=f"{'Disabled' if disabled else 'Enabled'} {target['username']!r}.")


@router.post("/admin/users/delete")
async def admin_delete_user(request: Request):
    """Permanently remove a user: drops the account row + GReader tokens and
    deletes the user's isolated data directory. Admin-only; cannot delete your
    own account or the last remaining admin."""
    if user_store is None:
        return Response(status_code=404)
    admin = _current_web_user(request)
    if not _is_web_admin(admin):
        return Response(status_code=403)
    form = await request.form()
    target_id = str(form.get("user_id") or "")
    if target_id == admin:
        return _account_redirect(error="You cannot delete your own account.")
    target = user_store.get_by_id(target_id)
    if target is None:
        return _account_redirect(error="No such user.")
    if target["is_admin"] and user_store.count_admins() <= 1:
        return _account_redirect(error="Cannot delete the last admin account.")
    try:
        delete_user_storage(target_id)
        user_store.delete_user(target_id)
    except Exception:
        LOGGER.exception("admin delete user failed for %r", target_id)
        return _account_redirect(error="Could not delete user (see server logs).")
    return _account_redirect(msg=f"Deleted user {target['username']!r} and all their data.")


@router.post("/admin/users/reset-password")
async def admin_reset_password(request: Request):
    if user_store is None:
        return Response(status_code=404)
    admin = _current_web_user(request)
    if not _is_web_admin(admin):
        return Response(status_code=403)
    form = await request.form()
    target_id = str(form.get("user_id") or "")
    new = str(form.get("new_password") or "")
    target = user_store.get_by_id(target_id)
    if target is None:
        return _account_redirect(error="No such user.")
    perr = _password_error(new)
    if perr:
        return _account_redirect(error=perr)
    user_store.set_password(target_id, new, scheme=PASSWORD_HASH_SCHEME)
    return _account_redirect(msg=f"Reset password for {target['username']!r}.")


@router.post("/admin/users/rename")
async def admin_rename_user(request: Request):
    if user_store is None:
        return Response(status_code=404)
    admin = _current_web_user(request)
    if not _is_web_admin(admin):
        return Response(status_code=403)
    form = await request.form()
    target_id = str(form.get("user_id") or "")
    new_username = str(form.get("new_username") or "").strip()
    target = user_store.get_by_id(target_id)
    if target is None:
        return _account_redirect(error="No such user.")
    old_username = target["username"]
    uerr = _username_error(new_username)
    if uerr:
        return _account_redirect(error=uerr)
    try:
        user_store.rename_user(target_id, new_username)
    except UserExistsError:
        return _account_redirect(error=f"Username {new_username!r} is already taken.")
    except ValueError:
        return _account_redirect(error="Invalid username — use 1–64 letters, digits, _ or -.")
    return _account_redirect(msg=f"Renamed {old_username!r} to {new_username!r} (data and tokens unchanged).")


@router.post("/admin/users/vacuum")
async def admin_vacuum_user(request: Request):
    """Admin action: VACUUM one user's reader/meta/starred DBs in the background.

    Nightly maintenance vacuums meta/starred but skips the reader DB (the big
    one); this reclaims space after a large purge or unsubscribe spree. A DB
    busy with another writer just logs and skips — the nightly pass retries."""
    if user_store is None:
        return Response(status_code=404)
    admin = _current_web_user(request)
    if not _is_web_admin(admin):
        return Response(status_code=403)
    form = await request.form()
    target_id = str(form.get("user_id") or "")
    if user_store.get_by_id(target_id) is None:
        return JSONResponse({"ok": False, "error": "No such user."}, status_code=404)

    def _vacuum() -> None:
        for label, path in [
            ("reader", tenancy.reader_db_path()),
            ("meta", tenancy.meta_db_path()),
            ("starred-archive", tenancy.starred_archive_db_path()),
        ]:
            try:
                p = Path(path)
                if not p.exists():
                    continue
                before = _dir_bytes(p)
                conn = sqlite3.connect(str(p), timeout=30)
                conn.execute("VACUUM")
                # In WAL mode the vacuum copies the whole rebuilt DB through the
                # -wal file, which then lingers at ~DB size while the app's
                # pooled connections keep the DB open — making the total LARGER
                # than before. Fold it back and truncate to zero.
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.close()
                LOGGER.info(
                    "[admin-vacuum] %s %s: %.1f MB -> %.1f MB (incl. sidecars)", target_id, label, before / 1048576, _dir_bytes(p) / 1048576
                )
            except Exception:
                LOGGER.exception("[admin-vacuum] %s %s failed", target_id, label)

    threading.Thread(target=_run_in_user_context, args=(target_id, _vacuum), daemon=True, name=f"admin-vacuum-{target_id[:12]}").start()
    return JSONResponse({"ok": True, "started": True})


@router.get("/admin/logs")
def admin_logs(request: Request, lines: int = 5000, level: str = "all", since: str = "", until: str = ""):
    """Tail the instance log for the Admin → Logs tab. Admin-only.

    ``since``/``until`` are optional timestamps (``YYYY-MM-DDTHH:MM`` from the
    datetime-local pickers) bounding the window; records outside it are dropped.
    They are interpreted in the SERVER's local timezone — the same frame the log
    file's own timestamps are written in — so the window lines up with what's in
    the file. (The pickers send the admin's browser-local time; on the normal
    self-hosted setup the admin and server share a timezone. A cross-timezone
    admin would see the window shifted by the offset — acceptable for this
    single-instance tool.) ``until`` is inclusive of its minute. ``lines`` caps
    the response (tail of the window) so a wide range can't dump the whole file;
    ``truncated`` flags when the cap dropped older matches so the UI can suggest
    narrowing."""
    if not _is_web_admin(_current_web_user(request)):
        return Response(status_code=403)
    max_lines = max(1, min(int(lines), 20000))
    min_level = "" if level.lower() == "all" else level
    since_dt, _ = _parse_local_ts(since)
    until_dt, until_had_secs = _parse_local_ts(until)
    if until_dt is not None and not until_had_secs:
        until_dt = until_dt.replace(second=59)  # minute-precision picker → inclusive
    log_lines, available, truncated = _read_log_tail(max_lines, min_level, since_dt, until_dt)
    return JSONResponse({"available": available, "lines": log_lines, "count": len(log_lines), "truncated": truncated})


# --- Debug / maintenance ---


@router.get("/debug/starred-archive/largest")
def debug_starred_archive_largest(limit: int = Query(default=50, ge=1, le=500)):
    if not DEBUG_MODE:
        return JSONResponse({"ok": False, "error": "Debug mode not enabled."}, status_code=403)
    rows = starred_archive_service.largest_archived_entries(limit=limit)
    # Annotate with entry titles where we still have them in the reader DB.
    titles: dict[tuple[str, str], str] = {}
    try:
        with get_reader() as reader:
            for row in rows:
                key = (row["feed_url"], row["entry_id"])
                try:
                    entry = reader.get_entry(key, None)
                except Exception:
                    entry = None
                if entry is not None:
                    titles[key] = str(getattr(entry, "title", "") or "")
    except Exception:
        pass
    for row in rows:
        row["title"] = titles.get((row["feed_url"], row["entry_id"]), "")
    return JSONResponse({"ok": True, "rows": rows})


@router.post("/debug/clear-lead-image-cache")
def debug_clear_lead_image_cache(
    request: Request,
    feed_url: str | None = Form(default=None),
):
    if not DEBUG_MODE:
        return JSONResponse({"ok": False, "error": "Debug mode not enabled."}, status_code=403)
    deleted, evicted_urls = lead_image_service.clear_lead_image_cache(feed_url or None)
    _purge_thumb_cache_for_urls(evicted_urls)
    return JSONResponse({"ok": True, "deleted": deleted, "feed_url": feed_url})


def _purge_thumb_cache_for_urls(urls: list[str]) -> None:
    """Delete /thumb cache entries for the given image URLs (DB + legacy files)."""
    _all_crops = list(_THUMB_COVER_POS) + ["contain", "smart"]
    keys: list[str] = []
    for url in urls:
        if not url:
            continue
        # Purge all crop variants (new format) plus the legacy no-crop key.
        for crop in _all_crops:
            keys.append(hashlib.sha256(f"{url}|{_THUMB_W}|{_THUMB_H}|{crop}".encode()).hexdigest())
        old_key = hashlib.sha256(f"{url}|{_THUMB_W}|{_THUMB_H}".encode()).hexdigest()
        keys.append(old_key)
        try:
            (THUMB_CACHE_DIR / f"{old_key}.jpg").unlink(missing_ok=True)
        except Exception:
            pass
    if keys:
        try:
            with get_thumb_connection() as conn:
                conn.executemany("DELETE FROM thumb_cache WHERE cache_key = ?", [(k,) for k in keys])
        except Exception:
            pass


@router.post("/debug/clear-entry-lead-image-cache")
def debug_clear_entry_lead_image_cache(
    request: Request,
    feed_url: str = Form(...),
    entry_id: str = Form(...),
):
    if not DEBUG_MODE:
        return JSONResponse({"ok": False, "error": "Debug mode not enabled."}, status_code=403)
    old_url = lead_image_service.clear_entry_lead_image_cache(feed_url, entry_id)
    if old_url:
        _purge_thumb_cache_for_urls([old_url])
    return JSONResponse({"ok": True, "cleared": old_url is not None})


@router.get("/debug/feed-bypass-state")
def debug_feed_bypass_state(request: Request, feed_url: str = Query(...)):
    if not DEBUG_MODE:
        return JSONResponse({"ok": False, "error": "Debug mode not enabled."}, status_code=403)
    return JSONResponse({"ok": True, "bypassed": feed_url in lead_image_service.get_bypassed_feeds()})


@router.post("/debug/toggle-feed-bypass")
def debug_toggle_feed_bypass(request: Request, feed_url: str = Form(...)):
    if not DEBUG_MODE:
        return JSONResponse({"ok": False, "error": "Debug mode not enabled."}, status_code=403)
    new_state = lead_image_service.toggle_feed_bypass(feed_url)
    return JSONResponse({"ok": True, "bypassed": new_state})
