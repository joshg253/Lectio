"""GReader API compatibility surface: `/greader/*` — auth, subscription
list/edit, unread counts, stream item ids/contents, tag edits, mark-all-read.

Stage 2 of the main.py route-by-URL-prefix split (Plan.md).
"""

from __future__ import annotations

import threading

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from main import (
    LOGGER,
    ROOT_FOLDER_NAME,
    _get_or_create_folder_by_name,
    _run_in_user_context,
    get_meta_connection,
    get_reader,
    get_root_folder_id,
    greader_service,
    invalidate_meta_structure_cache,
    tenancy,
    user_store,
)

router = APIRouter()


def _greader_token(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("GoogleLogin auth="):
        return auth[17:].strip()
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return request.query_params.get("token", "")


def _resolve_greader_user(request: Request) -> str | None:
    """Username authorized for this GReader request, or None."""
    token = _greader_token(request)
    return user_store.resolve_greader_token(token) if user_store else None


def _greader_ok(request: Request) -> bool:
    return _resolve_greader_user(request) is not None


@router.post("/greader/accounts/ClientLogin")
async def greader_login(request: Request) -> Response:
    """Authenticate and return a GReader auth token (username + API token)."""
    if user_store is None:
        return Response("Error=ServiceUnavailable\n", status_code=503)
    form = await request.form()
    email = str(form.get("Email") or form.get("email") or "")
    passwd = str(form.get("Passwd") or form.get("passwd") or "")
    local = email.split("@")[0] if "@" in email else email
    uid = user_store.verify_api_token(local, passwd)
    if not uid:
        return Response("Error=BadAuthentication\n", status_code=403)
    token = user_store.issue_greader_token(uid)
    return Response(f"SID={token}\nLSID={token}\nAuth={token}\n", media_type="text/plain")


@router.get("/greader/reader/api/0/user-info")
def greader_user_info(request: Request) -> Response:
    if not _greader_ok(request):
        return Response(status_code=401)
    display_name = None
    if user_store is not None:
        row = user_store.get_by_id(tenancy.current_user_id())
        display_name = row["username"] if row else tenancy.current_user_id()
    return JSONResponse(greader_service.get_user_info(display_name))  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]


@router.get("/greader/reader/api/0/tag/list")
def greader_tag_list(request: Request) -> Response:
    if not _greader_ok(request):
        return Response(status_code=401)
    return JSONResponse(greader_service.get_tag_list())  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]


@router.get("/greader/reader/api/0/subscription/list")
def greader_subscription_list(request: Request) -> Response:
    if not _greader_ok(request):
        return Response(status_code=401)
    return JSONResponse(greader_service.get_subscription_list())  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]


def _greader_label_name(label: str) -> str | None:
    """Extract the folder name from a GReader label id (user/-/label/<name>)."""
    marker = "/label/"
    i = label.find(marker)
    return label[i + len(marker) :].strip() if i >= 0 else None


def _greader_edit_subscriptions(streams: list[str], add_labels: list[str], remove_labels: list[str], new_title: str | None) -> None:
    """Apply a GReader subscription/edit to Lectio's single-folder model.

    ``a=user/-/label/<name>`` moves the feed into folder <name> (created if
    absent — GReader "add label" semantics); a lone ``r=user/-/label/<name>``
    removes it from that folder (→ Uncategorized). ``t=<title>`` renames the
    feed. Mirrors a web-UI move so synced clients (Capy, etc.) actually stick.
    """
    feed_urls = [s[len("feed/") :] for s in streams if s.startswith("feed/")]
    if not feed_urls:
        return
    add_folder = next((n for lab in add_labels if (n := _greader_label_name(lab))), None)
    remove_folders = [n for lab in remove_labels if (n := _greader_label_name(lab))]
    changed = False
    with get_reader() as reader:
        with get_meta_connection() as conn:
            root_id = get_root_folder_id(conn)
            for feed_url in feed_urls:
                if reader.get_feed(feed_url, None) is None:
                    continue
                if add_folder and add_folder != ROOT_FOLDER_NAME:
                    # Move: single folder, so clear existing membership first.
                    target_id = _get_or_create_folder_by_name(conn, add_folder)
                    conn.execute("DELETE FROM folder_feeds WHERE feed_url = ?", (feed_url,))
                    conn.execute(
                        "INSERT OR IGNORE INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)",
                        (target_id, feed_url),
                    )
                    changed = True
                elif remove_folders:
                    for name in remove_folders:
                        row = conn.execute(
                            "SELECT id FROM folders WHERE name = ? AND parent_id = ?",
                            (name, root_id),
                        ).fetchone()
                        if row:
                            conn.execute(
                                "DELETE FROM folder_feeds WHERE folder_id = ? AND feed_url = ?",
                                (int(row["id"]), feed_url),
                            )
                            changed = True
                if new_title:
                    try:
                        reader.set_feed_user_title(feed_url, new_title)
                        changed = True
                    except Exception:  # noqa: BLE001
                        LOGGER.warning("[greader] set_feed_user_title failed for %s", feed_url)
    if changed:
        invalidate_meta_structure_cache()


@router.post("/greader/reader/api/0/subscription/edit")
async def greader_subscription_edit(request: Request) -> Response:
    if not _greader_ok(request):
        return Response(status_code=401)
    form = await request.form()
    action = str(form.get("ac") or "edit")
    # We implement label (folder) edits + rename. subscribe/unsubscribe stay
    # no-op-OK (as before) so a client can't unsubscribe feeds unexpectedly.
    if action == "edit":
        _greader_edit_subscriptions(
            [v for v in form.getlist("s") if isinstance(v, str)],
            [v for v in form.getlist("a") if isinstance(v, str)],
            [v for v in form.getlist("r") if isinstance(v, str)],
            (lambda t: str(t) if isinstance(t, str) and t.strip() else None)(form.get("t")),
        )
    return Response("OK")


@router.post("/greader/reader/api/0/subscription/quickadd")
async def greader_subscription_quickadd(request: Request) -> Response:
    if not _greader_ok(request):
        return Response(status_code=401)
    return Response("OK")


@router.get("/greader/reader/api/0/unread-count")
def greader_unread_count(request: Request) -> Response:
    if not _greader_ok(request):
        return Response(status_code=401)
    return JSONResponse(greader_service.get_unread_counts())  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]


@router.get("/greader/reader/api/0/token")
def greader_action_token(request: Request) -> Response:
    if not _greader_ok(request):
        return Response(status_code=401)
    return Response(_greader_token(request), media_type="text/plain")


@router.get("/greader/reader/api/0/stream/items/ids")
def greader_stream_item_ids(request: Request) -> Response:
    if not _greader_ok(request):
        return Response(status_code=401)
    p = request.query_params
    stream_id = p.get("s", "user/-/state/com.google/reading-list")
    try:
        count = min(int(p.get("n", "20")), 10_000)
    except ValueError:
        count = 20
    continuation = p.get("c") or None
    xt_values = p.getlist("xt")
    exclude_read = "user/-/state/com.google/read" in xt_values
    start_time = int(p["ot"]) if "ot" in p else None
    stop_time = int(p["nt"]) if "nt" in p else None
    oldest_first = p.get("r") == "o"
    return JSONResponse(
        greader_service.get_stream_item_ids(  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]
            stream_id,
            count=count,
            continuation=continuation,
            exclude_read=exclude_read,
            start_time=start_time,
            stop_time=stop_time,
            oldest_first=oldest_first,
        )
    )


@router.post("/greader/reader/api/0/stream/items/contents")
async def greader_stream_items_contents(request: Request) -> Response:
    if not _greader_ok(request):
        return Response(status_code=401)
    form = await request.form()
    item_ids = [v for v in form.getlist("i") if isinstance(v, str)]
    return JSONResponse(greader_service.get_items_contents(item_ids))  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]


@router.get("/greader/reader/api/0/stream/contents/{stream_id:path}")
def greader_stream_contents_path(stream_id: str, request: Request) -> Response:
    if not _greader_ok(request):
        return Response(status_code=401)
    p = request.query_params
    if not stream_id:
        stream_id = p.get("s", "user/-/state/com.google/reading-list")
    try:
        count = min(int(p.get("n", "20")), 10_000)
    except ValueError:
        count = 20
    return JSONResponse(
        greader_service.get_stream_contents(  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]
            stream_id,
            count=count,
            continuation=p.get("c") or None,
            exclude_read="user/-/state/com.google/read" in p.getlist("xt"),
            oldest_first=p.get("r") == "o",
        )
    )


@router.get("/greader/reader/api/0/stream/contents")
def greader_stream_contents_query(request: Request) -> Response:
    stream_id = request.query_params.get("s", "user/-/state/com.google/reading-list")
    return greader_stream_contents_path(stream_id, request)


@router.post("/greader/reader/api/0/edit-tag")
async def greader_edit_tag(request: Request) -> Response:
    if not _greader_ok(request):
        return Response(status_code=401)
    form = await request.form()
    greader_service.edit_tag(  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]
        [v for v in form.getlist("i") if isinstance(v, str)],
        [v for v in form.getlist("a") if isinstance(v, str)],
        [v for v in form.getlist("r") if isinstance(v, str)],
    )
    return Response("OK")


@router.post("/greader/reader/api/0/mark-all-as-read")
async def greader_mark_all_as_read(request: Request) -> Response:
    if not _greader_ok(request):
        return Response(status_code=401)
    form = await request.form()
    stream_id = str(form.get("s") or "user/-/state/com.google/reading-list")
    ts_raw = form.get("ts")
    # ts is in microseconds; convert to seconds for the service.
    timestamp = int(str(ts_raw)) // 1_000_000 if ts_raw and isinstance(ts_raw, str) else None
    threading.Thread(
        target=_run_in_user_context,
        args=(tenancy.current_user_id(), greader_service.mark_all_as_read, stream_id, timestamp),  # ty: ignore[unresolved-attribute]
        daemon=True,
    ).start()
    return Response("OK")
