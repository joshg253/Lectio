"""Miniflux v1 API compatibility surface: `/v1/*` — auth, categories, feeds,
entries (list/detail/update), bookmark toggle.

Stage 2 of the main.py route-by-URL-prefix split (Plan.md).
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from main import miniflux_service, tenancy, user_store

router = APIRouter()


def _resolve_miniflux_user(request: Request) -> str | None:
    """Resolve a Miniflux X-Auth-Token header to a tenancy user_id.

    Returns the user_id on success, None on missing/invalid token."""
    if not user_store:
        return None
    token = request.headers.get("X-Auth-Token", "").strip()
    if not token:
        # Basic auth: username:api_token (some clients send token as password)
        import base64

        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode()
                _, _, token = decoded.partition(":")
                token = token.strip()
            except Exception:
                token = ""
    if not token:
        return None
    return user_store.miniflux_user_for_token(token)


def _miniflux_ok(request: Request) -> str | None:
    """Return the user_id if the request carries a valid Miniflux token, else None."""
    return _resolve_miniflux_user(request)


@router.get("/v1/version")
def miniflux_version() -> Response:
    """Version endpoint — some clients call this to detect Miniflux compatibility."""
    return JSONResponse({"version": "2.2.0"})


@router.post("/v1/auth/token")
async def miniflux_auth_token(request: Request) -> Response:
    """Token login endpoint for clients that prefer JWT-style auth.

    We return the user's api_token as a plain bearer token (not a real JWT)
    since we use X-Auth-Token for all subsequent requests."""
    if not user_store:
        return JSONResponse({"error_message": "Auth not available."}, status_code=503)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error_message": "Invalid JSON."}, status_code=400)
    username = str(body.get("username", ""))
    password = str(body.get("password", ""))
    uid = user_store.verify_login(username, password) if username and password else None
    if not uid:
        return JSONResponse({"error_message": "Invalid credentials."}, status_code=401)
    # Resolve the api_token directly from the auth DB
    with user_store._connect() as conn:
        row = conn.execute("SELECT api_token FROM users WHERE user_id=?", (uid,)).fetchone()
    if not row or not row["api_token"]:
        return JSONResponse({"error_message": "API token not set."}, status_code=403)
    return JSONResponse({"token": row["api_token"]})


@router.get("/v1/me")
def miniflux_me(request: Request) -> Response:
    uid = _miniflux_ok(request)
    if not uid:
        return JSONResponse({"error_message": "Access Unauthorized."}, status_code=401)
    assert user_store is not None  # _miniflux_ok only returns a uid when user_store is set
    with tenancy.user_context(uid):
        with user_store._connect() as conn:
            row = conn.execute("SELECT username, is_admin FROM users WHERE user_id=?", (uid,)).fetchone()
    if not row:
        return JSONResponse({"error_message": "User not found."}, status_code=404)
    return JSONResponse(
        {
            "id": 1,
            "username": row["username"],
            "is_admin": bool(row["is_admin"]),
            "theme": "system_serif",
            "language": "en_US",
            "timezone": "UTC",
            "entry_direction": "asc",
            "entries_per_page": 100,
            "keyboard_shortcuts": True,
            "show_reading_time": True,
            "entry_swipe": True,
            "gesture_nav": "tap",
            "last_login_at": datetime.now(timezone.utc).isoformat(),
            "display_mode": "standalone",
            "default_reading_speed": 265,
            "cjk_reading_speed": 500,
            "default_home_page": "unread",
            "categories_sorting_order": "alphabetical",
            "mark_read_on_view": False,
            "media_playback_rate": 1,
        }
    )


@router.get("/v1/categories")
def miniflux_categories(request: Request) -> Response:
    uid = _miniflux_ok(request)
    if not uid:
        return JSONResponse({"error_message": "Access Unauthorized."}, status_code=401)
    with tenancy.user_context(uid):
        cats = miniflux_service.get_categories()
    return JSONResponse(cats)


@router.get("/v1/feeds")
def miniflux_feeds(request: Request) -> Response:
    uid = _miniflux_ok(request)
    if not uid:
        return JSONResponse({"error_message": "Access Unauthorized."}, status_code=401)
    with tenancy.user_context(uid):
        feeds = miniflux_service.get_feeds()
    return JSONResponse(feeds)


def _miniflux_entries_response(request: Request, *, feed_id: int | None = None, category_id: int | None = None) -> Response:
    uid = _miniflux_ok(request)
    if not uid:
        return JSONResponse({"error_message": "Access Unauthorized."}, status_code=401)
    p = request.query_params
    status = p.get("status") or None
    starred_str = p.get("starred")
    starred: bool | None = None
    if starred_str == "true":
        starred = True
    elif starred_str == "false":
        starred = False
    try:
        limit = max(1, min(int(p.get("limit", 100)), 200))
        after_id = int(p["after_entry_id"]) if "after_entry_id" in p else None
        before_id = int(p["before_entry_id"]) if "before_entry_id" in p else None
    except ValueError, TypeError:
        return JSONResponse({"error_message": "Invalid parameter."}, status_code=400)
    direction = p.get("direction", "desc")
    with tenancy.user_context(uid):
        result = miniflux_service.get_entries(
            status=status,
            feed_id=feed_id,
            category_id=category_id,
            starred=starred,
            limit=limit,
            direction=direction,
            after_entry_id=after_id,
            before_entry_id=before_id,
        )
    return JSONResponse(result)


@router.get("/v1/entries")
def miniflux_entries(request: Request) -> Response:
    return _miniflux_entries_response(request)


@router.get("/v1/feeds/{feed_id}/entries")
def miniflux_feed_entries(feed_id: int, request: Request) -> Response:
    return _miniflux_entries_response(request, feed_id=feed_id)


@router.get("/v1/categories/{category_id}/entries")
def miniflux_category_entries(category_id: int, request: Request) -> Response:
    return _miniflux_entries_response(request, category_id=category_id)


@router.get("/v1/entries/{entry_id}")
def miniflux_entry(entry_id: int, request: Request) -> Response:
    uid = _miniflux_ok(request)
    if not uid:
        return JSONResponse({"error_message": "Access Unauthorized."}, status_code=401)
    with tenancy.user_context(uid):
        entry = miniflux_service.get_entry(entry_id)
    if entry is None:
        return JSONResponse({"error_message": "Entry not found."}, status_code=404)
    return JSONResponse(entry)


@router.put("/v1/entries")
async def miniflux_update_entries(request: Request) -> Response:
    uid = _miniflux_ok(request)
    if not uid:
        return JSONResponse({"error_message": "Access Unauthorized."}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error_message": "Invalid JSON."}, status_code=400)
    entry_ids = [int(x) for x in (body.get("entry_ids") or []) if str(x).isdigit() or isinstance(x, int)]
    status = str(body.get("status", ""))
    if not entry_ids or status not in ("read", "unread"):
        return JSONResponse({"error_message": "entry_ids and status (read|unread) required."}, status_code=400)
    with tenancy.user_context(uid):
        miniflux_service.update_entries(entry_ids, status)
    return Response(status_code=204)


@router.put("/v1/entries/{entry_id}/bookmark")
def miniflux_toggle_bookmark(entry_id: int, request: Request) -> Response:
    uid = _miniflux_ok(request)
    if not uid:
        return JSONResponse({"error_message": "Access Unauthorized."}, status_code=401)
    with tenancy.user_context(uid):
        result = miniflux_service.toggle_bookmark(entry_id)
    if result is None:
        return JSONResponse({"error_message": "Entry not found."}, status_code=404)
    return Response(status_code=204)
