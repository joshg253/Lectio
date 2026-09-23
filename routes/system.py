"""Low-traffic leaf endpoints: liveness/stats, auth, administration, dev feeds,
the /thumb proxy, starred assets, OPML/Takeout/Instapaper import-export, the
WebSub callback, and a handful of small single-route utility endpoints.

Stage 1 of the main.py route-by-URL-prefix split (Plan.md). These routes were
picked first because they are low-traffic and don't share meaningful state
with each other or with the rest of main.py.
"""

from __future__ import annotations

import hashlib
import io
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import httpx
from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from PIL import Image as _PILImage
from reader.exceptions import InvalidFeedURLError

from main import (
    _HOTLINK_REFUSAL_CODES,
    _THUMB_COVER_POS,
    _THUMB_FETCH_FAIL_CACHE,
    _THUMB_FETCH_FAIL_LOCK,
    _THUMB_FETCH_FAIL_TTL,
    _THUMB_H,
    _THUMB_RENDER_VERSION,
    _THUMB_W,
    AUTH_ENABLED,
    BASE_DIR,
    DEBUG_MODE,
    IMG_CACHE_DB_PATH,
    LECTIO_PUBLIC_URL,
    LOGGER,
    MANUAL_TAG_KEY_PREFIX,
    PASSWORD_HASH_SCHEME,
    READABILITY_USER_AGENT,
    SCHEDULER_STALL_SECONDS,
    SETTING_MAINTENANCE_HOUR,
    SETTING_SHARED_PINTEREST_OAUTH_CLIENT_ID,
    SETTING_SHARED_PINTEREST_OAUTH_CLIENT_SECRET,
    SETTING_SHARED_REDDIT_CLIENT_ID,
    SETTING_SHARED_REDDIT_CLIENT_SECRET,
    SETTING_SHARED_YT_OAUTH_CLIENT_ID,
    SETTING_SHARED_YT_OAUTH_CLIENT_SECRET,
    STATIC_ASSET_VERSION,
    THUMB_CACHE_DIR,
    THUMB_DB_PATH,
    YOUTUBE_SYNC_LAST_AT_KEY,
    YOUTUBE_SYNC_LAST_RESULT_KEY,
    _current_autofile_plan,
    _current_web_user,
    _devto_config_from_form,
    _dir_bytes,
    _flush_all_email_batches,
    _img_cache_get,
    _img_cache_key_url,
    _is_subscribable_feed_url,
    _login_failures,
    _login_failures_lock,
    _pinned_entry_thumb_response,
    _pinned_thumb_response,
    _run_automation_after_refresh,
    _run_youtube_sync,
    _same_origin_referer,
    _scheduler_stall_seconds,
    _scheduler_state,
    _scheduler_state_lock,
    _session_logged_in,
    attachment_filename_for_url,
    canonical_feed_url,
    devto_service,
    feed_refresh_service,
    format_datetime_for_ui,
    get_email_contacts,
    get_fetch_history_max_age_days,
    get_flaresolverr_url,
    get_img_cache_connection,
    get_img_cache_days,
    get_img_cache_max_dim,
    get_img_target_bytes,
    get_instance_default_auto_refresh,
    get_login_max_failures,
    get_login_window_seconds,
    get_meta_connection,
    get_proxy_mode,
    get_proxy_url,
    get_reader,
    get_resend_api_key,
    get_resend_from,
    get_root_folder_id,
    get_runtime_setting,
    get_setting,
    get_tailscale_url,
    get_thumb_connection,
    get_tombstone_sweep_days,
    invalidate_has_manual_tags_cache,
    invalidate_meta_structure_cache,
    invalidate_problematic_feeds_cache,
    invalidate_tag_counts_cache,
    invalidate_unread_counts_cache,
    lead_image_service,
    normalize_tag_value,
    saved_articles_service,
    saved_autofile_service,
    starred_archive_service,
    templates,
    tenancy,
    url_guard,
    user_store,
    websub_service,
)
from services import instapaper_import as instapaper_import_service
from services import takeout_service

router = APIRouter()


# ---------------------------------------------------------------------------
# Liveness / static / stats
# ---------------------------------------------------------------------------


@router.get("/healthz")
def healthz():
    """Liveness probe for reverse proxies (Traefik, etc.). Returns 200 as long
    as the process is serving requests. Intentionally does NOT touch the DB:
    under bulk-refresh load the meta DB can be locked for several seconds, and
    a probe that waits on it will time out and cause the proxy to withdraw the
    backend even though the app is still functioning.

    A stalled scheduler is reported in the body but does NOT fail the probe: this
    is also the Docker HEALTHCHECK and Traefik's, and a reader whose refresh is
    stuck is still perfectly usable for reading. Withdrawing the backend would
    turn a background-work failure into an outage.
    """
    body: dict[str, object] = {"status": "ok"}
    stalled_for = _scheduler_stall_seconds()
    with _scheduler_state_lock:
        body["scheduler"] = {
            "stage": _scheduler_state.get("stage"),
            "stalled_seconds": int(stalled_for) if stalled_for is not None else None,
            "stalled": stalled_for is not None and stalled_for >= SCHEDULER_STALL_SECONDS,
        }
    return JSONResponse(body)


@router.get("/sw.js")
def offline_service_worker():
    """Serve the offline worker from the ROOT path.

    A worker's default scope is its own directory, so /static/sw.js could only
    ever control /static/*. Read Mode lives at /read, so it has to be served from
    "/" (or carry Service-Worker-Allowed); the header is sent as well so the
    registration cannot silently fall back to a scope that controls nothing.

    Auth-exempt like the other static assets: the worker is fetched by the
    browser's registration machinery, and its own requests carry the session.
    """
    try:
        body = (BASE_DIR / "static" / "sw.js").read_text(encoding="utf-8")
    except Exception:
        return Response(status_code=404)
    return Response(
        body,
        media_type="application/javascript",
        headers={
            "Service-Worker-Allowed": "/",
            "Cache-Control": "no-store",
        },
    )


@router.get("/stats")
def get_stats():
    def _db_bytes(*paths: Path) -> int:
        total = 0
        for p in paths:
            for f in p.parent.glob(p.name + "*"):
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
        return total

    with get_meta_connection() as conn:
        feed_count = conn.execute("SELECT COUNT(DISTINCT feed_url) FROM folder_feeds").fetchone()[0]
        # Exclude the root folder from the folder count
        root_id = get_root_folder_id(conn)
        folder_count = conn.execute("SELECT COUNT(*) FROM folders WHERE id != ?", (root_id,)).fetchone()[0]
        saved_count = conn.execute("SELECT COUNT(*) FROM saved_entries").fetchone()[0]
        # Served fresh so the Stats tab reflects the latest (e.g. 3am) sync
        # without a full page reload — the rest of the grid already refreshes
        # here, but the YT-sync line used to be baked into the initial render.
        youtube_sync_last_at = get_setting(conn, YOUTUBE_SYNC_LAST_AT_KEY) or ""
        youtube_sync_last_result = get_setting(conn, YOUTUBE_SYNC_LAST_RESULT_KEY) or ""

    with get_reader() as reader:
        counts = reader.get_entry_counts()
        entry_total = counts.total
        entry_read = counts.read
        entry_unread = entry_total - entry_read

    reader_db_bytes = _db_bytes(tenancy.reader_db_path())
    meta_db_bytes = _db_bytes(tenancy.meta_db_path())
    thumb_db_bytes = _db_bytes(THUMB_DB_PATH)
    img_cache_db_bytes = _db_bytes(IMG_CACHE_DB_PATH)
    starred_archive_db_bytes = _db_bytes(tenancy.starred_archive_db_path())
    archive_stats = starred_archive_service.get_stats()

    thumb_count = 0
    try:
        with get_thumb_connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM thumb_cache").fetchone()
            thumb_count = int(row["c"]) if row else 0
    except Exception:
        pass

    img_cache_count = 0
    try:
        with get_img_cache_connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM img_cache").fetchone()
            img_cache_count = int(row["c"]) if row else 0
    except Exception:
        pass

    return JSONResponse(
        {
            "feed_count": feed_count,
            "folder_count": folder_count,
            "entry_total": entry_total,
            "entry_unread": entry_unread,
            "entry_read": entry_read,
            "entry_saved": saved_count,
            "reader_db_bytes": reader_db_bytes,
            "meta_db_bytes": meta_db_bytes,
            "thumb_db_bytes": thumb_db_bytes,
            "thumb_count": thumb_count,
            "img_cache_db_bytes": img_cache_db_bytes,
            "img_cache_count": img_cache_count,
            "starred_archive_db_bytes": starred_archive_db_bytes,
            "starred_archive_complete": archive_stats["complete"],
            "starred_archive_pending": archive_stats["pending"],
            "starred_archive_in_progress": archive_stats["in_progress"],
            "starred_archive_failed": archive_stats["failed"],
            "starred_archive_pending_removal": archive_stats["pending_removal"],
            "starred_archive_asset_count": archive_stats["asset_count"],
            "youtube_sync_last_at": youtube_sync_last_at,
            "youtube_sync_last_result": youtube_sync_last_result,
        }
    )


# ---------------------------------------------------------------------------
# Auth routes (/login, /logout)
# ---------------------------------------------------------------------------


@router.get("/login")
def login_page(request: Request, next: str = "/"):
    if not AUTH_ENABLED or _session_logged_in(request):
        return RedirectResponse(url=_safe_next(next), status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"next": next, "error": None, "static_asset_version": STATIC_ASSET_VERSION},
    )


def _client_ip_for_rate_limit(request: Request) -> str:
    """Best-effort client identifier for rate limiting.

    Uses request.client.host directly. When behind Traefik with
    LECTIO_HTTPS_ONLY=1, ProxyHeadersMiddleware rewrites client.host to the
    real client IP via X-Forwarded-For — see app.add_middleware setup above.
    """
    return request.client.host if request.client else "unknown"


def _login_attempt_blocked(ip: str, now: float) -> bool:
    if DEBUG_MODE:
        return False
    window = get_login_window_seconds()
    cutoff = now - window
    with _login_failures_lock:
        timestamps = [t for t in _login_failures.get(ip, []) if t >= cutoff]
        _login_failures[ip] = timestamps
        return len(timestamps) >= get_login_max_failures()


def _record_login_failure(ip: str, now: float) -> None:
    with _login_failures_lock:
        _login_failures.setdefault(ip, []).append(now)


def _clear_login_failures(ip: str) -> None:
    with _login_failures_lock:
        _login_failures.pop(ip, None)


def _safe_next(next_url: str | None) -> str:
    """Return ``next_url`` only if it is a safe same-origin path, else ``/``.

    Prevents post-login open redirects: rejects off-site absolute URLs and the
    protocol-relative (``//evil.com``) / backslash (``/\\evil.com``) forms that
    browsers normalise to an external authority.
    """
    if not next_url or not next_url.startswith("/"):
        return "/"
    if next_url.startswith("//") or next_url.startswith("/\\"):
        return "/"
    return next_url


@router.post("/login")
async def login_submit(request: Request, next: str = "/"):
    now = time.time()
    ip = _client_ip_for_rate_limit(request)
    if _login_attempt_blocked(ip, now):
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "next": next,
                "error": f"Too many failed login attempts. Try again in {get_login_window_seconds() // 60} minutes.",
                "static_asset_version": STATIC_ASSET_VERSION,
            },
            status_code=429,
        )

    form = await request.form()
    username = str(form.get("username") or "")
    password = str(form.get("password") or "")
    resolved = user_store.verify_login(username, password, default_scheme=PASSWORD_HASH_SCHEME) if user_store is not None else None
    if resolved is not None:
        _clear_login_failures(ip)
        request.session.clear()  # rotate session on login (anti-fixation)
        request.session["authenticated"] = True
        request.session["user_id"] = resolved
        return RedirectResponse(url=_safe_next(next), status_code=303)
    _record_login_failure(ip, now)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"next": next, "error": "Invalid username or password.", "static_asset_version": STATIC_ASSET_VERSION},
        status_code=401,
    )


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


# ---------------------------------------------------------------------------
# Administration page (GET only — the POST /account/*, /admin/* actions stay
# in main.py for now; they're Stage 5's routes/admin.py)
# ---------------------------------------------------------------------------


def _human_bytes(n: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    i = 0
    while f >= 1024 and i < len(units) - 1:
        f /= 1024
        i += 1
    return f"{f:.0f} {units[i]}" if i == 0 else f"{f:.1f} {units[i]}"


def _format_last_active(ts: float | None) -> str:
    if not ts:
        return "never"
    try:
        return format_datetime_for_ui(datetime.fromtimestamp(float(ts), tz=timezone.utc)) or "—"
    except Exception:
        return "—"


def _admin_user_rows() -> list[dict]:
    """User list for the Administration page, enriched with per-user stats
    (feed count, personal DB size, last-active). Each user's stats are read under
    its own tenancy context."""
    assert user_store is not None  # only called from administration_page, which already checked
    rows: list[dict] = []
    for u in user_store.list_users():
        uid = u["user_id"]
        feeds = 0
        db_bytes = 0
        try:
            with tenancy.user_context(uid):
                with get_meta_connection() as c:
                    feeds = int(c.execute("SELECT COUNT(DISTINCT feed_url) FROM folder_feeds").fetchone()[0])
            db_bytes = _dir_bytes(
                tenancy.reader_db_path(uid),
                tenancy.meta_db_path(uid),
                tenancy.starred_archive_db_path(uid),
            )
        except Exception:
            LOGGER.debug("admin stats failed for %r", uid, exc_info=True)
        rows.append(
            {
                **u,
                "feed_count": feeds,
                "db_human": _human_bytes(db_bytes),
                "last_active": _format_last_active(u.get("last_seen_at")),
            }
        )
    return rows


@router.get("/administration")
def administration_page(request: Request, msg: str | None = None, error: str | None = None):
    """Admin page: user management + instance configuration. Admin-only."""
    if user_store is None:
        return Response(status_code=404)
    uid = _current_web_user(request)
    if not uid:
        return RedirectResponse(url="/login?next=/administration", status_code=303)
    row = user_store.get_by_id(uid)
    if not row:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    if not row["is_admin"]:
        # Non-admins have no admin page; their per-user settings live in Settings.
        return RedirectResponse(url="/", status_code=303)

    def _masked(v: str) -> str:
        return "••••••••" if v else ""

    resend_key = get_resend_api_key()
    with get_meta_connection() as conn:
        maint_last = get_setting(conn, "maintenance_last_ran_at") or ""
    return templates.TemplateResponse(
        request,
        "administration.html",
        {
            "user_id": uid,
            "username": row["username"],
            "users": _admin_user_rows(),
            "message": msg,
            "error": error,
            # Instance configuration (admin-managed): email (Resend) + maintenance.
            "resend_key_set": bool(resend_key),
            "resend_key_masked": _masked(resend_key),
            "email_from": get_resend_from(),
            "maintenance_hour": get_runtime_setting(SETTING_MAINTENANCE_HOUR),
            "maintenance_last": maint_last,
            "img_cache_days": get_img_cache_days(),
            "img_cache_max_dim": get_img_cache_max_dim(),
            "img_target_bytes": get_img_target_bytes(),
            "proxy_url": get_proxy_url(),
            "proxy_mode": get_proxy_mode(),
            "tailscale_url": get_tailscale_url(),
            "flaresolverr_url": get_flaresolverr_url(),
            # Shared OAuth apps (stored in admin's own app_settings).
            "shared_yt_oauth_client_id": get_runtime_setting(SETTING_SHARED_YT_OAUTH_CLIENT_ID, ""),
            "shared_yt_oauth_client_secret_set": bool(get_runtime_setting(SETTING_SHARED_YT_OAUTH_CLIENT_SECRET)),
            "shared_yt_oauth_client_secret_masked": _masked(get_runtime_setting(SETTING_SHARED_YT_OAUTH_CLIENT_SECRET, "")),
            "shared_pinterest_oauth_client_id": get_runtime_setting(SETTING_SHARED_PINTEREST_OAUTH_CLIENT_ID, ""),
            "shared_pinterest_oauth_client_secret_set": bool(get_runtime_setting(SETTING_SHARED_PINTEREST_OAUTH_CLIENT_SECRET)),
            "shared_pinterest_oauth_client_secret_masked": _masked(get_runtime_setting(SETTING_SHARED_PINTEREST_OAUTH_CLIENT_SECRET, "")),
            "shared_reddit_client_id": get_runtime_setting(SETTING_SHARED_REDDIT_CLIENT_ID, ""),
            "shared_reddit_client_secret_set": bool(get_runtime_setting(SETTING_SHARED_REDDIT_CLIENT_SECRET)),
            "shared_reddit_client_secret_masked": _masked(get_runtime_setting(SETTING_SHARED_REDDIT_CLIENT_SECRET, "")),
            # Instance tuning
            "fetch_history_max_age_days": get_fetch_history_max_age_days(),
            "tombstone_sweep_days": get_tombstone_sweep_days(),
            "login_max_failures": get_login_max_failures(),
            "login_window_seconds": get_login_window_seconds(),
            "instance_auto_refresh": get_instance_default_auto_refresh(),
            "public_url": LECTIO_PUBLIC_URL,
            "static_asset_version": STATIC_ASSET_VERSION,
        },
    )


# ---------------------------------------------------------------------------
# Dev feeds (DEBUG_MODE only) — synthetic feeds + batch-flush for automation-rule testing
# ---------------------------------------------------------------------------


@router.get("/dev/feeds/email-match.xml")
@router.get("/dev/feeds/email-match.rss")
def dev_feed_email_match_rss():
    if not DEBUG_MODE:
        return Response(status_code=404)
    return _make_dev_feed("email-match", "Lectio Dev — Email Match (RSS)", "MATCH", 5, "rss")


@router.get("/dev/feeds/email-match.atom")
def dev_feed_email_match_atom():
    if not DEBUG_MODE:
        return Response(status_code=404)
    return _make_dev_feed("email-match", "Lectio Dev — Email Match (Atom)", "MATCH", 5, "atom")


@router.get("/dev/feeds/email-match.json")
def dev_feed_email_match_json():
    if not DEBUG_MODE:
        return Response(status_code=404)
    return _make_dev_feed("email-match", "Lectio Dev — Email Match (JSON Feed)", "MATCH", 5, "json")


@router.get("/dev/feeds/email-skip.xml")
@router.get("/dev/feeds/email-skip.rss")
def dev_feed_email_skip_rss():
    if not DEBUG_MODE:
        return Response(status_code=404)
    return _make_dev_feed("email-skip", "Lectio Dev — Email Skip (RSS)", "SKIP", 5, "rss")


@router.get("/dev/feeds/email-skip.atom")
def dev_feed_email_skip_atom():
    if not DEBUG_MODE:
        return Response(status_code=404)
    return _make_dev_feed("email-skip", "Lectio Dev — Email Skip (Atom)", "SKIP", 5, "atom")


@router.get("/dev/feeds/email-skip.json")
def dev_feed_email_skip_json():
    if not DEBUG_MODE:
        return Response(status_code=404)
    return _make_dev_feed("email-skip", "Lectio Dev — Email Skip (JSON Feed)", "SKIP", 5, "json")


def _make_dev_feed(feed_id: str, title: str, prefix: str, count: int, fmt: str) -> Response:
    """Generate a dev feed in RSS, Atom, or JSON Feed format.

    GUIDs are keyed to the current minute so all three formats produce identical
    entry IDs when fetched in the same minute — they all advance together.
    """
    import json as _json

    now_ts = time.time()
    minute = int(now_ts / 60) * 60  # floor to minute boundary

    entries = []
    for i in range(count):
        entry_minute = minute - i * 60
        entries.append(
            {
                "id": f"urn:lectio-dev:{feed_id}:{entry_minute}:{i}",
                "title": f"{prefix}: Dev article {i + 1} ({entry_minute})",
                "url": f"https://example.com/dev/{feed_id}/{entry_minute}/{i}",
                "iso": datetime.fromtimestamp(entry_minute, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "rfc": time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime(entry_minute)),
            }
        )

    if fmt == "atom":
        items_xml = ""
        for e in entries:
            items_xml += (
                f"<entry>"
                f"<id>{e['id']}</id>"
                f"<title>{e['title']}</title>"
                f"<link href='{e['url']}' rel='alternate'/>"
                f"<published>{e['iso']}</published>"
                f"<updated>{e['iso']}</updated>"
                f"<content type='text'>Dev test entry for rule testing.</content>"
                f"</entry>\n"
            )
        body = (
            "<?xml version='1.0' encoding='UTF-8'?>\n"
            "<feed xmlns='http://www.w3.org/2005/Atom'>\n"
            f"<title>{title}</title>\n"
            "<link href='https://example.com' rel='alternate'/>\n"
            f"<id>urn:lectio-dev:{feed_id}-atom</id>\n"
            f"<updated>{entries[0]['iso']}</updated>\n"
            f"{items_xml}"
            "</feed>"
        )
        return Response(content=body, media_type="application/atom+xml")

    if fmt == "json":
        items = [
            {
                "id": e["id"],
                "title": e["title"],
                "url": e["url"],
                "date_published": e["iso"],
                "content_text": "Dev test entry for rule testing.",
            }
            for e in entries
        ]
        body = _json.dumps(
            {
                "version": "https://jsonfeed.org/version/1.1",
                "title": title,
                "home_page_url": "https://example.com",
                "items": items,
            }
        )
        return Response(content=body, media_type="application/feed+json")

    # RSS 2.0 (default)
    items_xml = ""
    for e in entries:
        items_xml += (
            f"<item>"
            f"<title>{e['title']}</title>"
            f"<link>{e['url']}</link>"
            f"<guid isPermaLink='false'>{e['id']}</guid>"
            f"<pubDate>{e['rfc']}</pubDate>"
            f"<description>Dev test entry for rule testing.</description>"
            f"</item>\n"
        )
    body = (
        "<?xml version='1.0' encoding='UTF-8'?>\n"
        "<rss version='2.0'><channel>\n"
        f"<title>{title}</title>\n"
        "<link>https://example.com</link>\n"
        f"<description>{title}</description>\n"
        f"<lastBuildDate>{entries[0]['rfc']}</lastBuildDate>\n"
        f"{items_xml}"
        "</channel></rss>"
    )
    return Response(content=body, media_type="application/rss+xml")


@router.post("/dev/flush-email-batch")
def dev_flush_email_batch():
    if not DEBUG_MODE:
        return JSONResponse({"ok": False, "error": "Debug mode not enabled."}, status_code=403)
    try:
        _flush_all_email_batches()
        return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ---------------------------------------------------------------------------
# /thumb proxy
# ---------------------------------------------------------------------------

# Cap total time per /thumb fetch so one hanging host can't block a worker ~24s
# (httpx's float timeout applies per-phase, so 12.0 could mean connect+read = 24s).
_THUMB_FETCH_TIMEOUT = httpx.Timeout(6.0, connect=4.0)


def _thumb_fetch_recently_failed(url: str) -> bool:
    now = time.monotonic()
    with _THUMB_FETCH_FAIL_LOCK:
        exp = _THUMB_FETCH_FAIL_CACHE.get(url)
        if exp is None:
            return False
        if exp < now:
            _THUMB_FETCH_FAIL_CACHE.pop(url, None)
            return False
        return True


def _mark_thumb_fetch_failed(url: str) -> None:
    with _THUMB_FETCH_FAIL_LOCK:
        # Opportunistic prune so the dict can't grow unbounded.
        if len(_THUMB_FETCH_FAIL_CACHE) > 2000:
            now = time.monotonic()
            for k in [k for k, v in _THUMB_FETCH_FAIL_CACHE.items() if v < now]:
                _THUMB_FETCH_FAIL_CACHE.pop(k, None)
        _THUMB_FETCH_FAIL_CACHE[url] = time.monotonic() + _THUMB_FETCH_FAIL_TTL


@router.get("/thumb")
def thumbnail_proxy(
    url: str = Query(...), crop: str = Query(default="cover"), ms: str = Query(default=""), fz: str = Query(default="")
) -> Response:
    """Fetch a remote image, resize it to thumbnail dimensions with LANCZOS, and
    return a cached JPEG.  This eliminates the progressive-load flicker caused by
    downloading full-size hero images into the small post-list thumbnail slot."""
    # Sanitized inline-SVG lead images arrive as data:image/svg+xml URIs. There's
    # nothing to rasterize/crop (they're vector); decode and serve the SVG directly
    # so every /thumb consumer (post list, Feed Properties, previews) renders them.
    if url.startswith("data:image/svg+xml,"):
        svg = unquote(url[len("data:image/svg+xml,") :])
        return Response(
            content=svg,
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=604800, immutable"},
        )

    # A pinned feed thumbnail lives in our own cache, not on the network. The post list pipes every
    # thumbnail through /thumb, so without this branch the pinned copy fails the http(s) check below and
    # the feed renders no thumbnail at all — the bytes are there and nothing shows them. Served as-is, the
    # way the data: branch above is: these are already thumbnail-sized, and the crop is applied in CSS.
    if url.startswith("/api/feed-thumb?"):
        pinned_feed = parse_qs(urlparse(url).query).get("feed_url", [""])[0]
        if not pinned_feed:
            return Response(status_code=404)
        return _pinned_thumb_response(pinned_feed)

    # Same reasoning, per-entry: a signed lead-image URL (DeviantArt wixmp, etc.)
    # that was pinned during the enhance pass. See _pin_entry_thumbnail_bytes.
    if url.startswith("/api/entry-thumb?"):
        _qs = parse_qs(urlparse(url).query)
        pinned_entry_feed = _qs.get("feed_url", [""])[0]
        pinned_entry_id = _qs.get("entry_id", [""])[0]
        if not pinned_entry_feed or not pinned_entry_id:
            return Response(status_code=404)
        return _pinned_entry_thumb_response(pinned_entry_feed, pinned_entry_id)

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return Response(status_code=400)

    # Normalise: unknown values → cover; contain/smart use fit-scale (no crop).
    if crop not in _THUMB_COVER_POS and crop not in ("contain", "smart"):
        crop = "cover"

    # Per-feed SmartCrop min_scale arrives as the `ms` query param (clamped 0.5–1.0,
    # default 0.9). Set in Feed Properties; absent for feeds using the default.
    try:
        _smart_min_scale = min(1.0, max(0.5, float(ms or "0.9")))
    except ValueError, TypeError:
        _smart_min_scale = 0.9

    # Per-feed Fill zoom multiplier arrives as the `fz` query param (clamped 0.5–2.0,
    # default 1.0). Values < 1.0 show more of the image with black letterbox bars;
    # values > 1.0 crop more aggressively than the default tight fill.
    try:
        _fill_zoom = min(2.0, max(0.5, float(fz or "1.0")))
    except ValueError, TypeError:
        _fill_zoom = 1.0

    # "smart.2" busts old center-crop smart-mode entries when switching to content-aware crop.
    # Include min_scale / fill_zoom in the cache key so changing either busts stale thumbnails.
    if crop == "smart":
        _crop_cache_key = f"smart.2_m{_smart_min_scale:.2f}"
    elif crop in _THUMB_COVER_POS or crop == "cover":
        # "_p2" suffix busts entries cached before position-aware paste (zoom < 1 fix).
        _crop_cache_key = f"{crop}_z{_fill_zoom:.2f}" + ("_p2" if _fill_zoom < 1.0 else "")
    else:
        _crop_cache_key = crop
    cache_key = hashlib.sha256(f"{url}|{_THUMB_W}|{_THUMB_H}|{_crop_cache_key}|{_THUMB_RENDER_VERSION}".encode()).hexdigest()
    cached_headers = {"Cache-Control": "public, max-age=604800, immutable"}

    try:
        with get_thumb_connection() as conn:
            row = conn.execute("SELECT jpeg FROM thumb_cache WHERE cache_key = ?", (cache_key,)).fetchone()
        if row is not None:
            return Response(content=bytes(row["jpeg"]), media_type="image/jpeg", headers=cached_headers)
    except Exception:
        pass

    # Lazy migration: legacy on-disk cache from the per-file era. Promote to DB
    # and remove the file so the thumb_cache/ directory drains over time.
    legacy_path = THUMB_CACHE_DIR / f"{cache_key}.jpg"
    if legacy_path.exists():
        try:
            jpeg_bytes = legacy_path.read_bytes()
        except Exception:
            jpeg_bytes = None
        if jpeg_bytes:
            _store_thumb(cache_key, jpeg_bytes)
            try:
                legacy_path.unlink(missing_ok=True)
            except Exception:
                pass
            return Response(content=jpeg_bytes, media_type="image/jpeg", headers=cached_headers)

    # The image proxy's byte cache first — it very often already holds these
    # bytes, since the article view fetches the same URL through /api/img.
    #
    # This matters most for images behind short-lived signed URLs. That cache
    # keys on the URL with its signing params stripped (_img_cache_key_url), so
    # a copy fetched under any valid token answers forever — which is exactly
    # what keeps an expired DeviantArt URL rendering in the article. The
    # thumbnail path never consulted it, so it re-fetched the dead URL, got a
    # 401, and marked the host failed. The result was a post whose image
    # displayed fine and whose thumbnail never appeared, permanently, with a
    # slow failing fetch on every list render.
    raw: bytes | None = None
    src_content_type = ""
    try:
        _cached_src = _img_cache_get(hashlib.sha256(_img_cache_key_url(url).encode("utf-8")).hexdigest())
    except Exception:  # noqa: BLE001 — a cache miss is the safe answer
        _cached_src = None
    if _cached_src is not None:
        raw, src_content_type = _cached_src

    # Short-circuit hosts that just failed: avoids re-hitting (and blocking a worker
    # on) a folder full of server-blocked images on every page load. Checked after
    # the cache, so a cached image is still thumbnailed while its host is down.
    if raw is None and _thumb_fetch_recently_failed(url):
        return Response(status_code=502)

    if raw is None:
        try:
            # follow_redirects=False so url_guard.safe_get validates every hop
            # (SSRF: a public thumbnail URL must not redirect to an internal target).
            with url_guard.build_client(timeout=_THUMB_FETCH_TIMEOUT, headers={"User-Agent": READABILITY_USER_AGENT}) as client:
                _headers = {"User-Agent": READABILITY_USER_AGENT}
                resp = url_guard.safe_get(client, url, headers=_headers)
                # Hotlink protection: retry once with a same-origin Referer only after
                # an honest request is refused (see api_img_proxy / lead_images).
                if resp.status_code in _HOTLINK_REFUSAL_CODES:
                    _referer = _same_origin_referer(url)
                    if _referer:
                        resp = url_guard.safe_get(client, url, headers={**_headers, "Referer": _referer})
                if resp.status_code in (404, 410):
                    # Image is permanently gone — null it out so it isn't re-attempted
                    lead_image_service.invalidate_image_url(url)
                resp.raise_for_status()
                raw = resp.content
                src_content_type = resp.headers.get("content-type", "")
        except url_guard.UnsafeURLError:
            return Response(status_code=403)
        except Exception:
            _mark_thumb_fetch_failed(url)
            return Response(status_code=502)

    try:
        img = _PILImage.open(io.BytesIO(raw))
        # Flatten transparency onto WHITE before anything else. `.convert("RGB")`
        # alone keeps whatever RGB sits *under* the alpha, which is usually
        # black — so a transparent line-art PNG (xkcd/what-if illustrations,
        # logos, diagrams) became a black rectangle. Measured on
        # what-if.xkcd.com/imgs/a/138: mean luminance 33 the naive way against
        # 235 composited. The output is JPEG and shared across users and themes,
        # so a background has to be picked once; white is what this kind of
        # image is drawn for.
        _had_alpha = img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info
        if _had_alpha:
            _rgba = img.convert("RGBA")
            _flat = _PILImage.new("RGB", _rgba.size, (255, 255, 255))
            _flat.paste(_rgba, mask=_rgba.split()[3])
            img = _flat
        else:
            img = img.convert("RGB")
        iw, ih = img.size
        if crop == "smart":
            # Content-aware crop: use SmartCrop to find the most interesting
            # region at exactly the target dimensions. Falls back to the old
            # 40%-cap adaptive algorithm if the library is unavailable.
            _sc_done = False
            try:
                import smartcrop as _sc_mod

                # Downsample before analysis: SmartCrop's scoring is coarse
                # enough that 800px gives identical results at 2-3× less CPU.
                _SC_MAX = 800
                _sc_scale = min(1.0, _SC_MAX / max(iw, ih))
                _sc_img = (
                    img.resize(
                        (max(1, round(iw * _sc_scale)), max(1, round(ih * _sc_scale))),
                        _PILImage.Resampling.BILINEAR,
                    )
                    if _sc_scale < 1.0
                    else img
                )
                _sc_res = _sc_mod.SmartCrop().crop(_sc_img, _THUMB_W, _THUMB_H, min_scale=_smart_min_scale)
                _c = _sc_res["top_crop"]
                # Convert crop coords back to original image space then crop.
                _x1 = max(0, round(_c["x"] / _sc_scale))
                _y1 = max(0, round(_c["y"] / _sc_scale))
                _x2 = min(iw, round((_c["x"] + _c["width"]) / _sc_scale))
                _y2 = min(ih, round((_c["y"] + _c["height"]) / _sc_scale))
                img = img.crop((_x1, _y1, _x2, _y2))
                img = img.resize((_THUMB_W, _THUMB_H), _PILImage.Resampling.LANCZOS)
                _sc_done = True
            except Exception:
                pass
            if not _sc_done:
                _MAX_CROP = 0.4
                cover_s = max(_THUMB_W / iw, _THUMB_H / ih)
                contain_s = min(_THUMB_W / iw, _THUMB_H / ih)
                cap_w = _THUMB_W / (iw * (1.0 - _MAX_CROP))
                cap_h = _THUMB_H / (ih * (1.0 - _MAX_CROP))
                scale = max(contain_s, min(cover_s, cap_w, cap_h))
                new_w = max(1, round(iw * scale))
                new_h = max(1, round(ih * scale))
                img = img.resize((new_w, new_h), _PILImage.Resampling.LANCZOS)
                if new_w > _THUMB_W or new_h > _THUMB_H:
                    left = max(0, (new_w - _THUMB_W) // 2)
                    top = max(0, (new_h - _THUMB_H) // 2)
                    img = img.crop((left, top, left + min(new_w, _THUMB_W), top + min(new_h, _THUMB_H)))
        elif crop == "contain":
            # Scale to fit; CSS handles letterboxing / blurred backdrop.
            scale = min(_THUMB_W / iw, _THUMB_H / ih)
            new_w = max(1, round(iw * scale))
            new_h = max(1, round(ih * scale))
            img = img.resize((new_w, new_h), _PILImage.Resampling.LANCZOS)
            if new_w > _THUMB_W or new_h > _THUMB_H:
                left = max(0, (new_w - _THUMB_W) // 2)
                top = max(0, (new_h - _THUMB_H) // 2)
                img = img.crop((left, top, left + min(new_w, _THUMB_W), top + min(new_h, _THUMB_H)))
        else:
            scale = max(_THUMB_W / iw, _THUMB_H / ih) * _fill_zoom
            new_w = max(1, round(iw * scale))
            new_h = max(1, round(ih * scale))
            img = img.resize((new_w, new_h), _PILImage.Resampling.LANCZOS)
            if new_w >= _THUMB_W and new_h >= _THUMB_H:
                # Zoom ≥ 1.0: image fills frame — crop with anchor position.
                h_frac, v_frac = _THUMB_COVER_POS.get(crop, (0.5, 0.5))
                ex = max(0, new_w - _THUMB_W)
                ey = max(0, new_h - _THUMB_H)
                left = round(ex * h_frac)
                top = round(ey * v_frac)
                img = img.crop((left, top, left + _THUMB_W, top + _THUMB_H))
            else:
                # Zoom < 1.0: image smaller than frame — paste on a canvas at the
                # anchor position. Black letterboxes a photo; an image that came
                # with transparency was drawn to sit on a light page, and framing
                # it in black reintroduces exactly the black-box look the flatten
                # above just fixed.
                canvas = _PILImage.new(
                    "RGB",
                    (_THUMB_W, _THUMB_H),
                    (255, 255, 255) if _had_alpha else (0, 0, 0),
                )
                h_frac, v_frac = _THUMB_COVER_POS.get(crop, (0.5, 0.5))
                paste_left = round((_THUMB_W - new_w) * h_frac)
                paste_top = round((_THUMB_H - new_h) * v_frac)
                canvas.paste(img, (paste_left, paste_top))
                img = canvas
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85, optimize=True)
        jpeg_bytes = buf.getvalue()
        _store_thumb(cache_key, jpeg_bytes)
    except Exception:
        # Pillow failed (corrupt image, unsupported format, etc.) — serve original.
        return Response(
            content=raw,
            media_type=src_content_type or "image/jpeg",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    return Response(content=jpeg_bytes, media_type="image/jpeg", headers=cached_headers)


def _store_thumb(cache_key: str, jpeg_bytes: bytes) -> None:
    try:
        with get_thumb_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO thumb_cache (cache_key, jpeg, created_at) VALUES (?, ?, ?)",
                (cache_key, jpeg_bytes, time.time()),
            )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Starred asset (content-addressed attachment/image bytes)
# ---------------------------------------------------------------------------

# 64-char hex sha256 — the only shape we issue.
_ASSET_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


@router.get("/starred-asset/{asset_hash}")
def starred_asset(asset_hash: str) -> Response:
    if not _ASSET_HASH_RE.match(asset_hash):
        return Response(status_code=400)
    found = starred_archive_service.get_asset(asset_hash)
    if found is None:
        return Response(status_code=404)
    data, content_type = found
    headers = {"Cache-Control": "public, max-age=31536000, immutable"}
    # Name the file. The URL is a content hash, so without this a "Save link as"
    # — or any download the `download` attribute does not cover — writes a
    # 64-character hash with no extension. Images are left unnamed: they render
    # inline and a Content-Disposition would not help them.
    if not (content_type or "").lower().startswith(("image/", "audio/", "video/")):
        source_url = starred_archive_service.source_url_for_asset(asset_hash)
        if source_url:
            filename = attachment_filename_for_url(source_url)
            headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return Response(
        content=data,
        media_type=content_type,
        headers=headers,
    )


# ---------------------------------------------------------------------------
# YouTube folder sync / dev.to feed config
# ---------------------------------------------------------------------------


@router.post("/youtube/sync")
def youtube_sync_route(folder_id: int = Form(...)):
    result = _run_youtube_sync(folder_id=folder_id)
    if result["error"]:
        message = f"YouTube sync error: {result['error']}"
    else:
        message = f"YouTube sync: +{result['added']} / -{result['removed']} ({result['total']} subs)"
    return RedirectResponse(url=f"/?folder_id={folder_id}&message={message}", status_code=303)


@router.post("/devto-feeds/{feed_id}/config")
def update_devto_feed_config_route(
    feed_id: str,
    devto_tag: str = Form(""),
    devto_top_days: str = Form(""),
    devto_english_only: str = Form(""),
    devto_min_reactions: str = Form(""),
    devto_tags_exclude: str = Form(""),
):
    """Update a dev.to feed's filter config from the feed Properties modal."""
    config = _devto_config_from_form(devto_tag, devto_top_days, devto_english_only, devto_min_reactions, devto_tags_exclude)
    try:
        with get_meta_connection() as conn:
            with get_reader() as reader:
                devto_service.update_devto_feed_config(conn, reader, feed_id, config)
    except devto_service.DevToRateLimited:
        return JSONResponse({"error": "dev.to rate limit — try again in a bit."}, status_code=429)
    except ValueError:
        return JSONResponse({"error": "Feed not found."}, status_code=404)
    except Exception:  # noqa: BLE001 — details stay in the log, not the response
        LOGGER.exception("[devto] config update failed for %s", feed_id)
        return JSONResponse({"error": "Update failed — see server logs."}, status_code=502)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Email contacts
# ---------------------------------------------------------------------------


@router.get("/email-contacts")
def list_email_contacts_route():
    with get_meta_connection() as conn:
        return JSONResponse({"ok": True, "contacts": get_email_contacts(conn)})


def add_email_contact(conn, label: str, address: str) -> dict:
    label = label.strip()
    address = address.strip()
    if not label or not address or "@" not in address:
        raise ValueError("Invalid label or address")
    conn.execute(
        "INSERT OR IGNORE INTO email_contacts (label, address) VALUES (?, ?)",
        (label, address),
    )
    row = conn.execute("SELECT id, label, address FROM email_contacts WHERE address = ?", (address,)).fetchone()
    return dict(row)


def remove_email_contact(conn, contact_id: int) -> None:
    conn.execute("DELETE FROM email_contacts WHERE id = ?", (contact_id,))


@router.post("/email-contacts/add")
def add_email_contact_route(label: str = Form(...), address: str = Form(...)):
    try:
        with get_meta_connection() as conn:
            contact = add_email_contact(conn, label, address)
        return JSONResponse({"ok": True, "contact": contact})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@router.post("/email-contacts/remove")
def remove_email_contact_route(contact_id: int = Form(...)):
    with get_meta_connection() as conn:
        remove_email_contact(conn, contact_id)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# OPML export / import
# ---------------------------------------------------------------------------


def export_opml_text(conn) -> str:
    rows = conn.execute(
        """
        WITH RECURSIVE tree(id, name, parent_id, path) AS (
            SELECT id, name, parent_id, name as path
            FROM folders
            WHERE parent_id IS NULL
            UNION ALL
            SELECT f.id, f.name, f.parent_id, t.path || ' / ' || f.name
            FROM folders f
            JOIN tree t ON f.parent_id = t.id
        )
        SELECT id, name, parent_id FROM tree ORDER BY path
        """
    ).fetchall()

    folder_map = {
        int(row["id"]): {
            "name": row["name"],
            "parent_id": row["parent_id"],
            "children": [],
            "feeds": [],
        }
        for row in rows
    }

    for folder_id, data in folder_map.items():
        parent_id = data.get("parent_id")
        parent_idx = None
        if isinstance(parent_id, (int, str)):
            try:
                parent_idx = int(parent_id)
            except Exception:
                parent_idx = None
        if parent_idx is not None and parent_idx in folder_map:
            folder_map[parent_idx]["children"].append(folder_id)

    for row in conn.execute("SELECT folder_id, feed_url FROM folder_feeds ORDER BY feed_url"):
        try:
            fid = int(row["folder_id"])
        except Exception:
            continue
        folder_map[fid]["feeds"].append(str(row["feed_url"]))

    root_id = get_root_folder_id(conn)
    opml = ET.Element("opml", version="1.0")
    head = ET.SubElement(opml, "head")
    ET.SubElement(head, "title").text = "Lectio Export"
    ET.SubElement(head, "dateCreated").text = datetime.now(timezone.utc).isoformat()
    body = ET.SubElement(opml, "body")

    def add_folder(parent: ET.Element, folder_id: int) -> None:
        data = folder_map[folder_id]
        if folder_id != root_id:
            node = ET.SubElement(parent, "outline", text=str(data.get("name", "")), title=str(data.get("name", "")))
        else:
            node = parent

        for feed_url in data["feeds"]:
            ET.SubElement(
                node,
                "outline",
                type="rss",
                text=feed_url,
                title=feed_url,
                xmlUrl=feed_url,
            )

        for child_id in sorted(data["children"], key=lambda cid: str(folder_map[cid].get("name", "")).lower()):
            add_folder(node, child_id)

    add_folder(body, root_id)
    return ET.tostring(opml, encoding="utf-8", xml_declaration=True).decode("utf-8")


def import_opml(conn, opml_data: bytes) -> int:
    root = ET.fromstring(opml_data)
    body = root.find("body")
    if body is None:
        return 0

    root_id = get_root_folder_id(conn)
    imported = 0

    def ensure_folder(name: str, parent_id: int) -> int:
        name = name.strip() or "Untitled"
        row = conn.execute(
            "SELECT id FROM folders WHERE name = ? AND parent_id = ?",
            (name, parent_id),
        ).fetchone()
        if row:
            return int(row["id"])

        cursor = conn.execute(
            "INSERT INTO folders (name, parent_id) VALUES (?, ?)",
            (name, parent_id),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("Could not determine id for inserted folder.")
        return int(cursor.lastrowid)

    # Track feeds already assigned to a folder (including existing subscriptions).
    #
    # CANONICALIZED, because the incoming URL is canonicalized before it is looked
    # up here and comparing a canonical URL against raw stored ones finds nothing.
    # Any subscription whose stored URL is not already canonical — a trailing
    # slash is enough — then looked "new" and was subscribed a second time under
    # the canonical spelling. Re-importing Lectio's OWN export duplicated 440 of
    # 2,909 foldered feeds that way, which is precisely the restore-from-backup
    # path a user is most likely to take.
    feeds_with_folder = set(canonical_feed_url(str(row["feed_url"])) for row in conn.execute("SELECT feed_url FROM folder_feeds"))

    with get_reader() as reader:

        def walk(outline: ET.Element, target_folder_id: int, may_create_folder: bool) -> None:
            nonlocal imported
            feed_url = outline.attrib.get("xmlUrl")
            if feed_url:
                feed_url = feed_url.strip()
                if feed_url:
                    if not _is_subscribable_feed_url(feed_url):
                        # Reject non-http(s) schemes (e.g. file://) — reader would
                        # otherwise read local files when refreshing the feed.
                        LOGGER.warning("OPML import: skipping non-http(s) entry %r", feed_url)
                        return
                    # Canonicalize so a variant (old.reddit, ?alt=rss, trailing
                    # slash) attaches to an existing subscription instead of
                    # creating an uncategorized duplicate.
                    feed_url = canonical_feed_url(feed_url)
                    if feed_url in feeds_with_folder:
                        # Already assigned to a folder, skip
                        return
                    try:
                        reader.add_feed(feed_url, exist_ok=True)
                    except InvalidFeedURLError:
                        LOGGER.warning("OPML import: skipping non-URL entry %r", feed_url)
                        return
                    conn.execute(
                        "INSERT OR IGNORE INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)",
                        (target_folder_id, feed_url),
                    )
                    feeds_with_folder.add(feed_url)
                    imported += 1
                return

            folder_name = outline.attrib.get("text") or outline.attrib.get("title")
            folder_id = target_folder_id
            if may_create_folder and folder_name:
                # Lectio keeps a single folder level below the root.
                folder_id = ensure_folder(folder_name, root_id)

            for child in outline.findall("outline"):
                walk(child, folder_id, False)

        for outline in body.findall("outline"):
            walk(outline, root_id, True)

    return imported


@router.post("/opml/import")
async def opml_import(opml_file: Annotated[UploadFile, File(...)]):
    data = await opml_file.read()
    with get_meta_connection() as conn:
        imported = import_opml(conn, data)
        root_id = get_root_folder_id(conn)
    invalidate_meta_structure_cache()

    return RedirectResponse(
        url=f"/?folder_id={root_id}&message={quote_plus(f'Imported {imported} feed(s) from OPML.')}",
        status_code=303,
    )


@router.get("/opml/export")
def opml_export():
    with get_meta_connection() as conn:
        text = export_opml_text(conn)
    return Response(
        content=text,
        media_type="application/xml",
        headers={"Content-Disposition": "attachment; filename=lectio-export.opml"},
    )


# ---------------------------------------------------------------------------
# Takeout export / import
# ---------------------------------------------------------------------------

# Limit concurrent takeout exports to 1: each one reads the full reader DB + meta DB
# and builds an in-memory ZIP, so letting them pile up would multiply RAM + lock
# contention. OPML export is fast/small and is not gated by this semaphore.
_takeout_export_sem = threading.Semaphore(1)


@router.get("/takeout/export")
def takeout_export():
    if not _takeout_export_sem.acquire(blocking=False):
        return Response(
            content="An export is already in progress. Please wait and try again.",
            status_code=429,
            media_type="text/plain",
        )
    try:
        with get_meta_connection() as conn:
            opml_text = export_opml_text(conn)
            zip_bytes = takeout_service.build_takeout_zip(conn, tenancy.reader_db_path(), opml_text, app_version=STATIC_ASSET_VERSION)
        date_str = datetime.now().strftime("%Y%m%d")
        return Response(
            content=zip_bytes,
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename=lectio-takeout-{date_str}.zip"},
        )
    finally:
        _takeout_export_sem.release()


@router.post("/takeout/import")
async def takeout_import(request: Request, takeout_file: Annotated[UploadFile, File(...)]):
    data = await takeout_file.read()
    try:
        with get_meta_connection() as conn:
            summary = takeout_service.import_takeout_zip(conn, tenancy.reader_db_path(), data)
    except ValueError as exc:
        return RedirectResponse(
            url=f"/?message={quote_plus(str(exc))}",
            status_code=303,
        )
    parts = [f"{v} {k.replace('_', ' ')}" for k, v in summary.items() if v]
    msg = "Takeout imported: " + ", ".join(parts) if parts else "Takeout imported (nothing new to add)."
    return RedirectResponse(url=f"/?message={quote_plus(msg)}", status_code=303)


# ---------------------------------------------------------------------------
# Instapaper import
# ---------------------------------------------------------------------------


def _import_instapaper_for_current_user(data: bytes) -> dict:
    """Import an Instapaper CSV export into the Saved Items of the current user.

    Each bookmark becomes a starred entry in the Saved Articles feed with its
    original save time; Instapaper's Archive folder maps to Lectio's archived
    flag, custom folders and the Starred flag to manual tags. Readable content
    is NOT fetched inline — every item is enqueued to the starred-archive
    worker, which fetches and extracts pages offline (same as a bookmarklet
    save), so importing hundreds of URLs doesn't block or hammer sites.
    """
    plan = instapaper_import_service.plan_import(
        data,
        normalize_url=saved_articles_service.normalize_article_url,
        normalize_tag=normalize_tag_value,
    )
    summary = {"imported": 0, "duplicates": 0, "archived": 0, "tagged": 0}
    if not plan:
        return summary

    reader = get_reader()
    created_feed = saved_articles_service.ensure_saved_feed(reader)
    with get_meta_connection() as conn:
        for bm in plan:
            saved_dt = datetime.fromtimestamp(bm.saved_at, tz=timezone.utc) if bm.saved_at is not None else datetime.now(timezone.utc)
            existing = reader.get_entry((saved_articles_service.SAVED_FEED_URL, bm.url), None)
            if existing is None:
                entry: dict = {
                    "feed_url": saved_articles_service.SAVED_FEED_URL,
                    "id": bm.url,
                    "link": bm.url,
                    "title": bm.title,
                    # NOT saved_dt. An Instapaper CSV records when *you saved*
                    # the bookmark, never when the article was published — so
                    # this stored a 2015 article as published 2019 and the UI
                    # showed that as fact. Measured on the live library: 3,308
                    # entries whose publish date was exactly their save date.
                    # The save date is kept where it belongs, on saved_entries
                    # below. See UNKNOWN_PUBLISHED for why 1970 and not NULL.
                    "published": saved_articles_service.UNKNOWN_PUBLISHED,
                }
                try:
                    reader.add_entry(entry)
                except Exception:  # noqa: BLE001
                    LOGGER.exception("instapaper import: add_entry failed for %s", bm.url)
                    continue
                summary["imported"] += 1
            else:
                summary["duplicates"] += 1

            conn.execute(
                "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id, saved_at) VALUES (?, ?, ?)",
                (saved_articles_service.SAVED_FEED_URL, bm.url, saved_dt.isoformat()),
            )
            if bm.archived:
                # Instapaper's archived bookmarks import straight onto the done
                # axis. OR IGNORE, so re-importing never rewrites a date the
                # user has since set by archiving here.
                conn.execute(
                    "INSERT OR IGNORE INTO archived_entries (feed_url, entry_id, archived_at) VALUES (?, ?, ?)",
                    (saved_articles_service.SAVED_FEED_URL, bm.url, saved_dt.isoformat()),
                )
            if bm.archived:
                summary["archived"] += 1

            if bm.tags:
                # Presence-only manual tags; set_tag is idempotent, so this
                # merges with any tags the entry already carries.
                resource_id = (saved_articles_service.SAVED_FEED_URL, bm.url)
                for tag in bm.tags:
                    try:
                        reader.set_tag(resource_id, f"{MANUAL_TAG_KEY_PREFIX}{tag}")
                    except Exception:  # noqa: BLE001
                        LOGGER.warning("instapaper import: tag %r failed for %s", tag, bm.url)
                summary["tagged"] += 1

            try:
                starred_archive_service.enqueue_archive(saved_articles_service.SAVED_FEED_URL, bm.url)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("instapaper import: archive enqueue failed for %s: %s", bm.url, exc)
        conn.commit()

    if created_feed:
        invalidate_meta_structure_cache()
        invalidate_problematic_feeds_cache()
    invalidate_unread_counts_cache()
    if summary["tagged"]:
        invalidate_has_manual_tags_cache()
        invalidate_tag_counts_cache()

    # Run the autofile matcher over just what we imported, so the summary can
    # say how much of it belongs to feeds already subscribed to. An import that
    # lands silently in Uncategorized is how a 4,000-article backlog gets built
    # without anyone noticing.
    #
    # Deliberately reports rather than files. "Exactly one candidate feed" is
    # not the same as a trustworthy one — guitarplayer.com's only candidate was
    # a scraped single-article stub that would have swallowed 303 articles — so
    # filing stays behind the per-host review in Settings → Feeds, where the
    # evidence for each target is visible and nothing is pre-checked.
    try:
        imported_ids = {bm.url for bm in plan}
        matched, _marked, _titles = _current_autofile_plan(restrict_to=imported_ids)
        totals = saved_autofile_service.plan_totals(matched)
        summary["filable"] = totals["confident_articles"]
        summary["filable_hosts"] = totals["confident_hosts"]
    except Exception:  # noqa: BLE001 — a reporting extra must never fail an import
        LOGGER.exception("instapaper import: autofile match failed")
        summary["filable"] = 0
        summary["filable_hosts"] = 0
    return summary


@router.post("/instapaper/import")
async def instapaper_import(request: Request, instapaper_file: Annotated[UploadFile, File(...)]):
    """Import an Instapaper CSV export into Saved Items. Session-authenticated."""
    data = await instapaper_file.read()
    summary = _import_instapaper_for_current_user(data)
    if not any(summary.values()):
        msg = "No bookmarks found in that file. Use Instapaper's CSV export."
    else:
        bits = []
        if summary["imported"]:
            bits.append(f"{summary['imported']} imported")
        if summary["duplicates"]:
            bits.append(f"{summary['duplicates']} already saved")
        if summary["archived"]:
            bits.append(f"{summary['archived']} archived")
        if summary["tagged"]:
            bits.append(f"{summary['tagged']} tagged")
        msg = "Instapaper import: " + ", ".join(bits) + ". Article text is being fetched in the background."
        if summary.get("filable"):
            # Signpost the review rather than filing silently — see
            # _import_instapaper_for_current_user for why this doesn't auto-file.
            msg += (
                f" {summary['filable']} of these match feeds you already follow"
                f" ({summary['filable_hosts']} site(s)) — review under"
                " Settings → Feeds → Utilities → File saved articles."
            )
    return RedirectResponse(url=f"/?message={quote_plus(msg)}", status_code=303)


# ---------------------------------------------------------------------------
# WebSub callback routes
# ---------------------------------------------------------------------------


def _websub_verify_fanout(feed: str, hub_topic: str, challenge: str, lease: int | None) -> str | None:
    """Confirm a WebSub subscription handshake against the shared subscription store."""
    if websub_service is None:
        return None
    try:
        return websub_service.handle_verification(feed, hub_topic, challenge, lease)
    except Exception:
        LOGGER.exception("[websub] verification failed for %r", feed)
        return None


def _process_websub_push(feed: str, body: bytes, sig: str) -> None:
    """Fan a WebSub push out to every user subscribed to the topic.

    Verifies the push signature once against the shared subscription secret,
    then refreshes each subscriber's reader DB under its own tenancy context."""
    if not websub_service:
        return
    subscribers = websub_service.get_subscribers(feed)
    if not subscribers:
        return
    if not websub_service.verify_push_signature(feed, body, sig):
        LOGGER.warning("[websub] push for %s failed signature check; ignoring", feed)
        return
    for uid in subscribers:
        with tenancy.user_context(uid):
            try:
                feed_refresh_service.update_feeds([feed])
                # Run automation (mark-read, tag-filter, dedup, …) on the pushed
                # entries — every other refresh path does this, but a WebSub push
                # would otherwise deliver entries that bypass all rules. Feeds
                # from prolific WebSub publishers (e.g. realpython.com) arrive
                # almost entirely via push, so skipping this meant their rules
                # effectively never fired.
                _run_automation_after_refresh({feed})
                invalidate_unread_counts_cache()
            except Exception:
                LOGGER.exception("[websub] push refresh failed for user %r", uid)


@router.get("/websub/callback")
def websub_verify(
    feed: str = Query(default=""),
    hub_mode: str = Query(default="", alias="hub.mode"),
    hub_topic: str = Query(default="", alias="hub.topic"),
    hub_challenge: str = Query(default="", alias="hub.challenge"),
    hub_lease_seconds: int | None = Query(default=None, alias="hub.lease_seconds"),
):
    """Hub challenge-response verification (step 2 of subscribe handshake)."""
    if not websub_service or not feed or hub_mode != "subscribe" or not hub_challenge:
        return Response(status_code=404)
    challenge = _websub_verify_fanout(feed, hub_topic, hub_challenge, hub_lease_seconds)
    if challenge is None:
        return Response(status_code=404)
    return Response(content=challenge, media_type="text/plain")


@router.post("/websub/callback")
async def websub_push(request: Request, feed: str = Query(default="")):
    """Receive a push notification from a WebSub hub."""
    if not websub_service or not feed:
        return Response(status_code=400)
    body = await request.body()
    sig = request.headers.get("x-hub-signature-256") or request.headers.get("x-hub-signature", "")
    # Fan out across every subscribing user in the background (signature is
    # verified per-user inside), so the push refreshes each subscriber's own
    # reader through the normal pipeline (dedup, automation, lead images). The
    # hub only needs a prompt 2xx ack, so don't block on the work.
    threading.Thread(
        target=_process_websub_push,
        args=(feed, body, sig),
        daemon=True,
    ).start()
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Misc internal
# ---------------------------------------------------------------------------


@router.post("/internal/warm-lead-image-cache")
def internal_warm_lead_image_cache():
    """Reload the lead-image cache from the meta DB.

    Useful for forcing the running process to pick up lead images that
    were backfilled or modified on disk without restarting the server.
    """
    try:
        lead_image_service.warm_cache_from_db()
        # Expose how many entries are now cached for quick verification.
        cached = getattr(lead_image_service, "_cache", {})
        sample_keys = list(cached.keys())[:5]
        return JSONResponse({"status": "ok", "cached": len(cached), "sample": sample_keys})
    except Exception as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)
