"""Highlight/automation rule CRUD: `GET /highlights`, `GET /highlights/suggestions`,
`POST /highlights/merge-group`, `POST /highlights/merge-group-regex-convert`,
`POST /highlights/add`, `POST /highlights/edit`, `POST /highlights/remove`,
`POST /highlights/toggle`, `POST /highlights/reorder`.

Stage 3 of the main.py route-by-URL-prefix split (Plan.md).

The rule-scanning/mutation helpers these routes call (get_highlight_keywords,
add_highlight_keyword, remove_highlight_keyword, find_mergeable_rule_groups,
find_redundant_feed_rules, find_regex_convertible_rule_groups,
merge_highlight_rule_group, merge_regex_convertible_rule_group) stay in main.py:
they are also called from services/automation_rules.py, other main.py-resident
routes, scripts/backfill_missed_youtube_playlist_adds.py, and are exercised
directly (as main.<name>) by tests/integration/test_mergeable_rules.py and
several rule-type test files. `_validate_highlight_rule` and
`_highlight_rule_response` moved here since both callers (add/edit) moved too.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse

from main import (
    _DEDUP_FUZZY_PCT_DEFAULT,
    _DEDUP_MIN_TITLE_WORDS,
    _DEDUP_VALID_MATCH_METHODS,
    _HIGHLIGHT_VALID_SCOPES,
    _clamp_fuzzy_pct,
    _clamp_min_title_words,
    add_highlight_keyword,
    find_mergeable_rule_groups,
    find_redundant_feed_rules,
    find_regex_convertible_rule_groups,
    get_highlight_keywords,
    get_meta_connection,
    is_instapaper_configured,
    is_quire_connected,
    merge_highlight_rule_group,
    merge_regex_convertible_rule_group,
    parse_feeds_scope_id,
    parse_tag_filter_spec,
    quire_project_oid,
    remove_highlight_keyword,
    url_guard,
    youtube_oauth_connected,
)
from services.webhooks import WEBHOOK_VALID_FORMATS

router = APIRouter()


@router.get("/highlights")
def get_highlights_route():
    with get_meta_connection() as conn:
        rows = get_highlight_keywords(conn)
    return JSONResponse({"ok": True, "keywords": rows})


@router.get("/highlights/suggestions")
def get_highlight_suggestions_route():
    """Rules that could be one rule (Settings → Automation → Rules), plus feed
    rules already redundant under a covering folder rule. Suggestion-with-
    preview, never automatic — see Plan.md "Utilities: find rules that could
    be one rule"."""
    with get_meta_connection() as conn:
        mergeable, mismatched = find_mergeable_rule_groups(conn)
        redundant = find_redundant_feed_rules(conn)
        regex_convertible = find_regex_convertible_rule_groups(conn)
    return JSONResponse(
        {
            "ok": True,
            "mergeable": mergeable,
            "mismatched": mismatched,
            "redundant": redundant,
            "regex_convertible": regex_convertible,
        }
    )


@router.post("/highlights/merge-group")
def merge_highlight_group_route(
    type: str = Form(...),
    scope: str = Form(...),
    scope_id: str = Form(""),
    search_in: str = Form("title"),
    is_regex: int = Form(0),
    color: str = Form(""),
    delivery: str = Form(""),
    email_to: str = Form(""),
    batch_time: str = Form(""),
    batch_count: int = Form(0),
    cc_me: int = Form(0),
):
    with get_meta_connection() as conn:
        result = merge_highlight_rule_group(
            conn,
            type,
            scope,
            scope_id,
            search_in,
            bool(is_regex),
            color,
            delivery,
            email_to,
            batch_time,
            batch_count,
            bool(cc_me),
        )
    if result is None:
        return JSONResponse(
            {"ok": False, "error": "Nothing to merge — the group changed since this was suggested."},
            status_code=409,
        )
    return JSONResponse({"ok": True, **result})


@router.post("/highlights/merge-group-regex-convert")
def merge_highlight_group_regex_convert_route(
    type: str = Form(...),
    scope: str = Form(...),
    scope_id: str = Form(""),
    search_in: str = Form("title"),
):
    """Merge a group spanning the is_regex boundary — see
    find_regex_convertible_rule_groups / merge_regex_convertible_rule_group.
    No is_regex form field: unlike /highlights/merge-group, the group by
    definition isn't one is_regex value."""
    with get_meta_connection() as conn:
        result = merge_regex_convertible_rule_group(conn, type, scope, scope_id, search_in)
    if result is None:
        return JSONResponse(
            {"ok": False, "error": "Nothing to merge — the group changed since this was suggested."},
            status_code=409,
        )
    return JSONResponse({"ok": True, **result})


def _validate_highlight_rule(
    scope: str, scope_id: str, keyword: str, rule_type: str, yt_playlist_id: str, webhook_url: str, webhook_format: str
) -> str | None:
    """Shared by /highlights/add and /highlights/edit — an error message, or
    None if the rule is valid. One copy so a new rule type's validation can't
    drift between the two routes. *keyword* and *webhook_url* are expected
    already stripped."""
    if scope not in _HIGHLIGHT_VALID_SCOPES:
        return "invalid scope"
    if rule_type == "deduplicate":
        if keyword not in _DEDUP_VALID_MATCH_METHODS:
            return "invalid match method for deduplicate rule"
        if scope == "feed":
            return "deduplicate needs at least two feeds — select multiple feeds or a folder"
        if scope == "feeds" and len(parse_feeds_scope_id(scope_id)) < 2:
            return "deduplicate needs at least two feeds selected"
    elif rule_type == "youtube_playlist":
        # Keyword is optional for this rule (empty = add every new video in scope).
        if not yt_playlist_id.strip():
            return "a target playlist is required"
        if not youtube_oauth_connected():
            return "connect a YouTube account first"
    elif rule_type == "instapaper":
        # Keyword is optional (blank = save every new entry in scope).
        if not is_instapaper_configured():
            return "configure Instapaper first"
    elif rule_type == "quire":
        # Keyword is optional (blank = add every new entry in scope).
        if not is_quire_connected():
            return "connect Quire first"
        if not quire_project_oid():
            return "pick a Quire destination project in Settings first"
    elif rule_type == "save_article":
        # Keyword is optional (blank = star every new entry in scope), matching
        # the other save-out rules (instapaper/quire/youtube_playlist).
        pass
    elif rule_type == "tag_filter":
        # keyword holds the +include/-exclude spec; it must yield >=1 valid tag.
        _req, _good, _exc = parse_tag_filter_spec(keyword)
        if not _req and not _good and not _exc:
            return "at least one tag is required (e.g. +python, -rust)"
    else:
        if not keyword:
            return "keyword is required"
    if rule_type == "webhook":
        if not webhook_url or not url_guard.is_safe_outbound_url(webhook_url):
            return "a valid public webhook URL is required"
        if webhook_format not in WEBHOOK_VALID_FORMATS:
            return "invalid webhook format"
    return None


def _highlight_rule_response(
    scope,
    scope_id,
    keyword,
    color,
    is_regex,
    type,
    search_in,
    delivery,
    email_to,
    batch_time,
    batch_count,
    cc_me,
    enabled,
    dedup_window_hours,
    exclude_scope_ids,
    dedup_fuzzy_pct,
    dedup_min_title_words,
    webhook_url,
    webhook_format,
    webhook_batch,
    yt_playlist_id,
    yt_playlist_title,
    yt_include_shorts,
    yt_mark_read,
    yt_min_minutes,
    yt_max_minutes,
    label="",
) -> JSONResponse:
    """Shared by /highlights/add and /highlights/edit — the saved rule, in the
    shape the client's rule list expects. *keyword* and *webhook_url* are
    expected already stripped."""
    return JSONResponse(
        {
            "ok": True,
            "scope": scope,
            "scope_id": scope_id,
            "keyword": keyword,
            "color": color,
            "is_regex": bool(is_regex),
            "type": type,
            "search_in": search_in,
            "delivery": delivery,
            "email_to": email_to,
            "batch_time": batch_time,
            "batch_count": batch_count,
            "cc_me": bool(cc_me),
            "enabled": bool(enabled),
            "dedup_window_hours": dedup_window_hours,
            "dedup_fuzzy_pct": _clamp_fuzzy_pct(dedup_fuzzy_pct),
            "dedup_min_title_words": _clamp_min_title_words(dedup_min_title_words),
            "exclude_scope_ids": exclude_scope_ids.strip(),
            "webhook_url": webhook_url,
            "webhook_format": webhook_format,
            "webhook_batch": bool(webhook_batch),
            "yt_playlist_id": yt_playlist_id.strip(),
            "yt_playlist_title": yt_playlist_title.strip(),
            "yt_include_shorts": bool(yt_include_shorts),
            "yt_mark_read": bool(yt_mark_read),
            "yt_min_minutes": max(0, int(yt_min_minutes or 0)),
            "yt_max_minutes": max(0, int(yt_max_minutes or 0)),
            "label": label.strip(),
        }
    )


@router.post("/highlights/add")
def add_highlight_route(
    scope: str = Form(...),
    scope_id: str = Form(""),
    # Optional at the form layer (a youtube_playlist rule allows a blank keyword =
    # "all videos"); per-type validation below still requires it for other types.
    keyword: str = Form(""),
    color: str = Form("yellow"),
    is_regex: int = Form(0),
    type: str = Form("highlight"),
    search_in: str = Form("title"),
    delivery: str = Form("immediately"),
    email_to: str = Form(""),
    batch_time: str = Form(""),
    batch_count: int = Form(0),
    cc_me: int = Form(0),
    enabled: int = Form(0),
    dedup_window_hours: int = Form(168),
    exclude_scope_ids: str = Form(""),
    dedup_fuzzy_pct: int = Form(_DEDUP_FUZZY_PCT_DEFAULT),
    dedup_min_title_words: int = Form(_DEDUP_MIN_TITLE_WORDS),
    webhook_url: str = Form(""),
    webhook_format: str = Form("generic"),
    webhook_batch: int = Form(0),
    yt_playlist_id: str = Form(""),
    yt_playlist_title: str = Form(""),
    yt_include_shorts: int = Form(0),
    yt_mark_read: int = Form(1),
    yt_min_minutes: int = Form(0),
    yt_max_minutes: int = Form(0),
    label: str = Form(""),
):
    keyword = keyword.strip()
    webhook_url = webhook_url.strip()
    err = _validate_highlight_rule(scope, scope_id, keyword, type, yt_playlist_id, webhook_url, webhook_format)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    with get_meta_connection() as conn:
        add_highlight_keyword(
            conn,
            scope,
            scope_id,
            keyword,
            color,
            bool(is_regex),
            type,
            search_in,
            delivery,
            email_to,
            batch_time,
            batch_count,
            bool(cc_me),
            enabled,
            dedup_window_hours,
            exclude_scope_ids,
            _clamp_fuzzy_pct(dedup_fuzzy_pct),
            _clamp_min_title_words(dedup_min_title_words),
            webhook_url,
            webhook_format,
            bool(webhook_batch),
            yt_playlist_id,
            yt_playlist_title,
            bool(yt_include_shorts),
            bool(yt_mark_read),
            yt_min_minutes,
            yt_max_minutes,
            label=label,
        )
    return _highlight_rule_response(
        scope,
        scope_id,
        keyword,
        color,
        is_regex,
        type,
        search_in,
        delivery,
        email_to,
        batch_time,
        batch_count,
        cc_me,
        enabled,
        dedup_window_hours,
        exclude_scope_ids,
        dedup_fuzzy_pct,
        dedup_min_title_words,
        webhook_url,
        webhook_format,
        webhook_batch,
        yt_playlist_id,
        yt_playlist_title,
        yt_include_shorts,
        yt_mark_read,
        yt_min_minutes,
        yt_max_minutes,
        label=label,
    )


@router.post("/highlights/edit")
def edit_highlight_route(
    old_scope: str = Form(...),
    old_scope_id: str = Form(""),
    old_keyword: str = Form(""),
    scope: str = Form(...),
    scope_id: str = Form(""),
    keyword: str = Form(""),
    color: str = Form("yellow"),
    is_regex: int = Form(0),
    type: str = Form("highlight"),
    search_in: str = Form("title"),
    delivery: str = Form("immediately"),
    email_to: str = Form(""),
    batch_time: str = Form(""),
    batch_count: int = Form(0),
    cc_me: int = Form(0),
    enabled: int = Form(0),
    dedup_window_hours: int = Form(168),
    exclude_scope_ids: str = Form(""),
    dedup_fuzzy_pct: int = Form(_DEDUP_FUZZY_PCT_DEFAULT),
    dedup_min_title_words: int = Form(_DEDUP_MIN_TITLE_WORDS),
    webhook_url: str = Form(""),
    webhook_format: str = Form("generic"),
    webhook_batch: int = Form(0),
    yt_playlist_id: str = Form(""),
    yt_playlist_title: str = Form(""),
    yt_include_shorts: int = Form(0),
    yt_mark_read: int = Form(1),
    yt_min_minutes: int = Form(0),
    yt_max_minutes: int = Form(0),
    label: str = Form(""),
):
    """Atomic counterpart to the client's remove-then-add edit flow.

    A rule's identity is (scope, scope_id, keyword). Editing used to be two
    separate requests against /highlights/remove and /highlights/add; when the
    identity was unchanged there was nothing to remove (INSERT OR REPLACE
    overwrites in place), but sending both anyway raced the add against the
    remove — the add landed first and the remove then deleted it, 20 times out
    of 20 in a local reproduction, both responses OK so the UI reported
    success. Josh lost a Deals dedup rule to this 2026-08-20 (a dedup rule
    hits it on every edit, since its match method IS the keyword). Client-side
    sequencing (await the remove before the add, skip it entirely when the
    identity didn't change) closed the hole the same day; this closes it
    structurally, in one transaction, so no future caller can reopen it."""
    keyword = keyword.strip()
    old_keyword = old_keyword.strip()
    webhook_url = webhook_url.strip()
    err = _validate_highlight_rule(scope, scope_id, keyword, type, yt_playlist_id, webhook_url, webhook_format)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    with get_meta_connection() as conn:
        same_identity = scope == old_scope and str(scope_id or "") == str(old_scope_id or "") and keyword == old_keyword
        # INSERT OR REPLACE below re-creates the row (new rowid) even when the
        # identity is unchanged, so the prior rule_uid must be fetched and
        # carried forward regardless -- otherwise every edit would mint a new
        # one, defeating the point (see rule_uid's ensure_meta_schema comment).
        old_row = conn.execute(
            "SELECT rule_uid FROM highlight_keywords WHERE scope = ? AND scope_id = ? AND keyword = ?",
            (old_scope, old_scope_id, old_keyword),
        ).fetchone()
        rule_uid = str(old_row["rule_uid"]) if old_row else ""
        if not same_identity:
            remove_highlight_keyword(conn, old_scope, old_scope_id, old_keyword)
        add_highlight_keyword(
            conn,
            scope,
            scope_id,
            keyword,
            color,
            bool(is_regex),
            type,
            search_in,
            delivery,
            email_to,
            batch_time,
            batch_count,
            bool(cc_me),
            enabled,
            dedup_window_hours,
            exclude_scope_ids,
            _clamp_fuzzy_pct(dedup_fuzzy_pct),
            _clamp_min_title_words(dedup_min_title_words),
            webhook_url,
            webhook_format,
            bool(webhook_batch),
            yt_playlist_id,
            yt_playlist_title,
            bool(yt_include_shorts),
            bool(yt_mark_read),
            yt_min_minutes,
            yt_max_minutes,
            rule_uid,
            label,
        )
    return _highlight_rule_response(
        scope,
        scope_id,
        keyword,
        color,
        is_regex,
        type,
        search_in,
        delivery,
        email_to,
        batch_time,
        batch_count,
        cc_me,
        enabled,
        dedup_window_hours,
        exclude_scope_ids,
        dedup_fuzzy_pct,
        dedup_min_title_words,
        webhook_url,
        webhook_format,
        webhook_batch,
        yt_playlist_id,
        yt_playlist_title,
        yt_include_shorts,
        yt_mark_read,
        yt_min_minutes,
        yt_max_minutes,
        label=label,
    )


@router.post("/highlights/remove")
def remove_highlight_route(
    scope: str = Form(...),
    scope_id: str = Form(""),
    # Optional: youtube_playlist rules can have a blank keyword ("all videos"), so
    # remove/toggle (and the edit flow, which is remove+add) must accept "".
    keyword: str = Form(""),
):
    with get_meta_connection() as conn:
        remove_highlight_keyword(conn, scope, scope_id, keyword)
    return JSONResponse({"ok": True})


@router.post("/highlights/toggle")
def toggle_highlight_route(
    scope: str = Form(...),
    scope_id: str = Form(""),
    keyword: str = Form(""),
    enabled: int = Form(...),
):
    with get_meta_connection() as conn:
        conn.execute(
            "UPDATE highlight_keywords SET enabled = ? WHERE scope = ? AND scope_id = ? AND keyword = ?",
            (1 if enabled else 0, scope, scope_id, keyword.strip()),
        )
    return JSONResponse({"ok": True, "enabled": bool(enabled)})


@router.post("/highlights/reorder")
async def reorder_highlights_route(request: Request):
    body = await request.json()
    order: list[dict] = body.get("order", [])
    with get_meta_connection() as conn:
        for i, item in enumerate(order):
            conn.execute(
                "UPDATE highlight_keywords SET sort_order = ? WHERE scope = ? AND scope_id = ? AND keyword = ?",
                (i, item.get("scope", ""), item.get("scope_id", ""), item.get("keyword", "")),
            )
    return JSONResponse({"ok": True})
