"""Fever API compatibility surface: `GET /fever`, `POST /fever` (both routed
through the same shared handler, `_fever_handler`).

Stage 2 of the main.py route-by-URL-prefix split (Plan.md).
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from main import LOGGER, fever_service, tenancy, user_store

router = APIRouter()


async def _fever_handler(request: Request) -> Response:
    """Shared handler for GET and POST requests to the Fever API endpoint."""
    if not fever_service:
        return JSONResponse({"api_version": 3, "auth": 0}, status_code=503)

    query_params = dict(request.query_params)
    form_data: dict = {}
    if request.method == "POST":
        try:
            body = await request.form()
            form_data = dict(body)
        except Exception:
            pass
    params = {**query_params, **form_data}

    if "api" not in params:
        return Response(status_code=404)

    api_key = params.get("api_key", "")
    # Resolve the api_key (md5(username:api_token)) to a user_id and bind
    # the tenancy context so the dispatch reads that user's data.
    uid = user_store.fever_user_for_key(api_key) if user_store else None
    if not uid:
        return JSONResponse({"api_version": 3, "auth": 0})
    with tenancy.user_context(uid):
        return JSONResponse(_fever_build_result(params))


def _fever_build_result(params: dict) -> dict:
    """Build the Fever response for an authenticated request. Runs under the
    caller's tenancy context (the bound user in multi mode)."""
    assert fever_service is not None  # caller already checked
    result: dict = {
        "api_version": 3,
        "auth": 1,
        "last_refreshed_on_time": int(time.time()),
    }

    # Mark actions (processed before data requests).
    if "mark" in params:
        mark_type = params.get("mark", "")
        action = params.get("as", "")
        try:
            item_id_raw = params.get("id", "")
            before_raw = params.get("before", "0")
            if mark_type == "item" and item_id_raw:
                fever_service.mark_item(int(item_id_raw), action)
            elif mark_type == "feed" and action == "read":
                fever_service.mark_feed_read(int(item_id_raw), int(before_raw))
            elif mark_type == "group" and action == "read":
                fever_service.mark_group_read(int(item_id_raw), int(before_raw))
        except ValueError, Exception:
            pass

    # Data requests.
    if "feeds" in params or "groups" in params:
        data = fever_service.get_feeds_and_groups()
        result["feeds_groups"] = data["feeds_groups"]
        if "feeds" in params:
            result["feeds"] = data["feeds"]
        if "groups" in params:
            result["groups"] = data["groups"]

    if "items" in params:
        try:
            since_id = int(params["since_id"]) if "since_id" in params else None
            max_id = int(params["max_id"]) if "max_id" in params else None
            with_ids = params.get("with_ids")
            result.update(fever_service.get_items(since_id=since_id, max_id=max_id, with_ids=with_ids))
        except (ValueError, Exception) as exc:
            LOGGER.warning("[fever] get_items error: %s", exc)

    if "unread_item_ids" in params:
        result["unread_item_ids"] = fever_service.get_unread_item_ids()

    if "saved_item_ids" in params:
        result["saved_item_ids"] = fever_service.get_saved_item_ids()

    if "links" in params:
        result["links"] = []

    if "favicons" in params:
        result["favicons"] = []

    return result


@router.get("/fever")
async def fever_get(request: Request) -> Response:
    """Fever API endpoint (GET) — used by some clients for initial auth checks."""
    return await _fever_handler(request)


@router.post("/fever")
async def fever_post(request: Request) -> Response:
    """Fever API endpoint (POST) — primary method used by Fever-compatible clients."""
    return await _fever_handler(request)
