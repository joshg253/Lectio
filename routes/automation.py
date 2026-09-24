"""Automation surface: `POST /rules/webhook-test`, `GET /rules/tag-vocabulary`,
`GET /rules/dry-run`, `POST /rules/tag-filter/toggle`, `POST /rules/run-now`,
`GET /automation/history`, `GET /automation/history/{log_id}/entries`,
`GET /dedup/false-matches`, `POST /dedup/false-match`.

Stage 4 of the main.py route-by-URL-prefix split (Plan.md). `/entries/feed-tags`
sits in this same main.py region but is an entries concern (late chip delivery
for the entry pane), not automation, and stays in main.py.

`_run_tag_filter`, `_run_now_dedup`, `_run_now_pattern` come straight from services/automation_rules.py. `_dry_run_dedup` and
`_dry_run_pattern` (the /rules/dry-run preview engine) live here, each with exactly one caller; the dedup preview and Run Now are thin
wrappers over the shared matching engine in services/dedup.py.

`resolve_rule_feed_urls`, `toggle_feed_tag_filter`, `feed_tag_service`, and the
rest of the generic dedup/tag-scope helpers stay in main.py: they're shared
with services/automation_rules.py and/or other still-in-main.py routes
(toggle_feed_tag_filter's own `_run_tag_filter` call at the feed-tag-chip
toggle path, for one).
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import JSONResponse

from main import (
    _DEDUP_FUZZY_PCT_DEFAULT,
    _DEDUP_MIN_TITLE_WORDS,
    _DEDUP_VALID_MATCH_METHODS,
    _clamp_min_title_words,
    _dedup_fuzzy_threshold,
    _is_youtube_short,
    _resolve_dedup_feed_urls,
    build_keyword_matcher,
    entry_effective_date,
    feed_display_title,
    feed_tag_service,
    get_meta_connection,
    get_reader,
    normalize_tag_value,
    parse_folders_scope_id,
    resolve_rule_feed_urls,
    toggle_feed_tag_filter,
    url_guard,
    youtube_duration_service,
)
from services import dedup
from services.automation_rules import _dedup_false_matches, _run_now_dedup, _run_now_pattern, _run_tag_filter
from services.webhooks import WEBHOOK_VALID_FORMATS, build_webhook_payload, send_webhook

router = APIRouter()


@router.post("/rules/webhook-test")
def webhook_test_route(
    webhook_url: str = Form(...),
    webhook_format: str = Form("generic"),
):
    """Send a sample payload to a webhook URL so the user can verify the endpoint
    (IFTTT applet, Zapier hook, etc.) without waiting for a matching entry at the
    next refresh. SSRF-guarded by send_webhook."""
    webhook_url = webhook_url.strip()
    if not webhook_url or not url_guard.is_safe_outbound_url(webhook_url):
        return JSONResponse({"ok": False, "error": "a valid public webhook URL is required"}, status_code=400)
    if webhook_format not in WEBHOOK_VALID_FORMATS:
        webhook_format = "generic"
    sample = {
        "feed_url": "https://example.com/feed",
        "entry_id": "lectio-webhook-test",
        "title": "Lectio webhook test",
        "link": "https://example.com/test-article",
        "feed_title": "Lectio Test Feed",
        "excerpt": "This is a test payload sent from Lectio to verify your webhook.",
        "published": datetime.now().isoformat(),
        "tags": ["lectio", "test"],
    }
    ok, err = send_webhook(webhook_url, build_webhook_payload(sample, webhook_format))
    if ok:
        return JSONResponse({"ok": True})
    return JSONResponse({"ok": False, "error": err or "send failed"}, status_code=400)


@router.get("/rules/tag-vocabulary")
def rules_tag_vocabulary_route(
    scope: str = Query("global"),
    scope_id: str = Query(""),
    limit: int = Query(400),
):
    """The feed-provided tags available to a tag_filter rule in this scope.

    Feeds the rule form's autocomplete. A tag_filter spec can only ever match
    what ingest captured into ``entry_feed_tags``, so typing blind against a
    140-tag long tail (HackerNoon) or a hyphenated stored form (`windows-11`)
    is guesswork; this turns it into a list.

    Tags come back **normalized**, the same transform ``parse_tag_filter_spec``
    applies to what the user types — so completing a suggestion produces a
    token that matches by construction. Counts are entry counts, merged across
    casing variants, and are the reason to pick one tag over another.
    """
    with get_meta_connection() as conn:
        feed_urls = resolve_rule_feed_urls(conn, scope, scope_id)
    raw = feed_tag_service.tag_vocabulary(feed_urls, limit=max(1, min(limit, 2000)))
    merged: dict[str, int] = {}
    for tag, count in raw:
        normalized = normalize_tag_value(tag)
        if normalized:
            merged[normalized] = merged.get(normalized, 0) + count
    tags = sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))
    return JSONResponse({"tags": [{"tag": t, "count": n} for t, n in tags]})


def _dry_run_dedup(
    conn: sqlite3.Connection,
    scope: str,
    scope_id: str,
    match_method: str,
    window_hours: int,
    max_entries: int = 5000,
    exclude_scope_ids: str = "",
    custom_feed_urls: set[str] | None = None,
    fuzzy_threshold: float = 0.80,
    min_title_words: int = _DEDUP_MIN_TITLE_WORDS,
) -> dict:
    """Preview which entries a deduplicate rule would mark read."""
    feed_urls = _resolve_dedup_feed_urls(conn, scope, scope_id, exclude_scope_ids, custom_feed_urls)
    if isinstance(feed_urls, dict):
        return feed_urls  # {"error": ...}
    if len(feed_urls) < 2:
        return {
            "groups": [],
            "total_entries_scanned": 0,
            "total_would_mark_read": 0,
            "message": "Need at least 2 feeds in scope to deduplicate",
        }
    false_matches = _dedup_false_matches(conn) if match_method == "safe" else set()
    with get_reader() as reader:
        records = dedup.collect_records(
            reader,
            feed_urls,
            per_feed_limit=max(1, max_entries // max(1, len(feed_urls))),
            read=None,
            feed_title=lambda f: feed_display_title(f, str(f.url)),
            effective_date=entry_effective_date,
            max_total=max_entries,
            safe_fields=match_method == "safe",
        )
    groups = dedup.find_groups(
        records,
        match_method,
        window_hours=window_hours,
        fuzzy_threshold=fuzzy_threshold,
        min_title_words=min_title_words,
        false_matches=false_matches,
    )
    return {
        "groups": groups,
        "total_entries_scanned": len(records),
        "total_would_mark_read": sum(len(g["mark_read"]) for g in groups),
        "total_unread_would_mark_read": dedup.unread_actionable(groups),
    }


def _dry_run_pattern(
    conn: sqlite3.Connection,
    scope: str,
    scope_id: str,
    keyword: str,
    is_regex: bool,
    search_in: str,
    max_entries: int = 1000,
    result_limit: int = 20,
    match_all_if_empty: bool = False,
    exclude_shorts: bool = False,
    min_secs: int = 0,
    max_secs: int = 0,
    unread_only: bool = False,
) -> dict:
    """Preview which entries a pattern-based rule would affect (read + unread, newest first
    -- unless ``unread_only``, see below).

    ``match_all_if_empty`` supports rules whose keyword is an optional filter (e.g.
    youtube_playlist: a blank keyword means "every entry in scope"). ``exclude_shorts``
    drops YouTube Shorts from the preview so it matches what a youtube_playlist rule
    with Include-Shorts off would actually add.

    ``unread_only``: mark_as_read (like _run_now_pattern, its actual apply path) only
    ever acts on unread entries, and the default read+unread scan here is ordered by
    recency and capped at max_entries -- on a large, active library a genuinely-unread
    but no-longer-recent entry sits well past that cap and the preview silently never
    reaches it, even though Run Now (unbounded, read=False) would have caught it fine.
    Found 2026-09-02: a global-scope rule's preview reported no match for an article
    ranked ~19,600th by recency in a 123k-entry library. When set, scans read=False
    with no cap at all -- exactly _run_now_pattern's own scan -- trading "preview can
    be slow on a huge unread backlog" for "preview mustn't lie about what Run Now will
    actually do." Other rule types sharing this function (highlight, in particular)
    legitimately care about already-read entries too, so this defaults off."""

    if not keyword:
        if not match_all_if_empty:
            return {"matches": [], "total_scanned": 0, "total_matches": 0, "truncated": False}

        def match_fn(text):
            return True
    else:
        try:
            match_fn = build_keyword_matcher(keyword, is_regex)
        except re.error as e:
            # Same module object the matcher compiles with — `_re` is an alias
            # some of these functions import locally, and mixing the two names
            # made it look like the handler might not catch.
            return {"error": f"Invalid regex: {e}"}

    if scope == "folder":
        try:
            int(scope_id)
        except ValueError, TypeError:
            return {"error": "invalid scope_id"}
    elif scope == "folders" and not parse_folders_scope_id(scope_id):
        return {"error": "invalid scope_id"}
    feed_urls: set[str] | None = resolve_rule_feed_urls(conn, scope, scope_id)

    matches: list[dict] = []
    total_scanned = 0
    total_matches = 0

    with get_reader() as reader:
        feed_title_map = {str(f.url): feed_display_title(f, str(f.url)) for f in reader.get_feeds()}

        def iter_entries():
            if unread_only:
                # Mirrors _run_now_pattern's own iter_unread exactly: no limit, no
                # per-feed split -- a capped/split scan is exactly what missed the
                # match this option exists to fix.
                if feed_urls is None:
                    yield from reader.get_entries(read=False)
                else:
                    for furl in feed_urls:
                        yield from reader.get_entries(feed=furl, read=False)
            elif feed_urls is None:
                yield from reader.get_entries(limit=max_entries)
            elif len(feed_urls) == 1:
                yield from reader.get_entries(feed=next(iter(feed_urls)), limit=max_entries)
            else:
                per_feed = max(1, max_entries // len(feed_urls))
                for furl in feed_urls:
                    yield from reader.get_entries(feed=furl, limit=per_feed)

        for entry in iter_entries():
            if not unread_only and total_scanned >= max_entries:
                break
            if exclude_shorts and _is_youtube_short(entry):
                continue
            if min_secs or max_secs:
                # Duration filter preview: use the entry's primary video (its link).
                _vid = youtube_duration_service.extract_video_id(str(entry.link or ""))
                _dur = youtube_duration_service.get_cached_duration(_vid)[0] if _vid else None
                if _dur is None or (min_secs and _dur < min_secs) or (max_secs and _dur > max_secs):
                    continue
            total_scanned += 1
            title_text = str(entry.title or "")
            body_text = ""
            if search_in in ("body", "both"):
                for c in entry.content or []:
                    body_text += (c.value or "") + " "
                body_text += str(entry.summary or "")

            if search_in == "title":
                matched = match_fn(title_text)
            elif search_in == "body":
                matched = match_fn(body_text)
            else:
                matched = match_fn(title_text) or match_fn(body_text)

            if matched:
                total_matches += 1
                if len(matches) < result_limit:
                    published = entry_effective_date(entry)
                    matches.append(
                        {
                            "title": title_text,
                            "link": str(entry.link or ""),
                            "feed_url": str(entry.feed_url or ""),
                            "feed_title": feed_title_map.get(str(entry.feed_url or ""), str(entry.feed_url or "")),
                            "published": published.isoformat() if published else None,
                            "read": bool(entry.read),
                        }
                    )

    return {
        "matches": matches,
        "total_scanned": total_scanned,
        "total_matches": total_matches,
        "truncated": total_matches > result_limit,
        "unread_only": unread_only,
    }


@router.get("/rules/dry-run")
def rules_dry_run_route(
    type: str = Query("highlight"),
    scope: str = Query("global"),
    scope_id: str = Query(""),
    keyword: str = Query(""),
    is_regex: int = Query(0),
    search_in: str = Query("title"),
    dedup_window_hours: int = Query(168),
    exclude_scope_ids: str = Query(""),
    fuzzy_pct: int = Query(_DEDUP_FUZZY_PCT_DEFAULT),
    min_title_words: int = Query(_DEDUP_MIN_TITLE_WORDS),
    feed_urls: str = Query(""),  # comma-separated; overrides scope for dedup
    yt_include_shorts: int = Query(1),
    yt_min_minutes: int = Query(0),
    yt_max_minutes: int = Query(0),
):
    with get_meta_connection() as conn:
        if type == "tag_filter":
            result = _run_tag_filter(conn, scope, scope_id, keyword, apply=False)
        elif type == "deduplicate":
            match_method = keyword if keyword in _DEDUP_VALID_MATCH_METHODS else "slug"
            custom: set[str] | None = None
            if feed_urls:
                custom = {u.strip() for u in feed_urls.split(",") if u.strip()}
            result = _dry_run_dedup(
                conn,
                scope,
                scope_id,
                match_method,
                max(1, dedup_window_hours),
                exclude_scope_ids=exclude_scope_ids,
                custom_feed_urls=custom,
                fuzzy_threshold=_dedup_fuzzy_threshold(fuzzy_pct),
                min_title_words=_clamp_min_title_words(min_title_words),
            )
        elif type in ("highlight", "mark_as_read", "email_article", "webhook", "youtube_playlist", "instapaper", "quire", "save_article"):
            # youtube_playlist's keyword is an optional filter — a blank keyword
            # previews every entry in scope (all videos); Shorts are excluded unless
            # the rule opts in, matching what the rule would actually add.
            _is_yt = type == "youtube_playlist"
            # The save-out rules (yt/instapaper/quire/save_article) treat a blank
            # keyword as "all in scope".
            result = _dry_run_pattern(
                conn,
                scope,
                scope_id,
                keyword,
                bool(is_regex),
                search_in,
                match_all_if_empty=(_is_yt or type in ("instapaper", "quire", "save_article")),
                exclude_shorts=(_is_yt and not yt_include_shorts),
                min_secs=(max(0, yt_min_minutes) * 60 if _is_yt else 0),
                max_secs=(max(0, yt_max_minutes) * 60 if _is_yt else 0),
                unread_only=(type == "mark_as_read"),
            )
        else:
            return JSONResponse({"error": "unknown rule type"}, status_code=400)
    if "error" in result:
        return JSONResponse({"error": result["error"]}, status_code=400)
    result["ok"] = True
    result["type"] = type
    return JSONResponse(result)


@router.post("/rules/tag-filter/toggle")
def tag_filter_toggle_route(
    feed_url: str = Form(...),
    tag: str = Form(...),
    sign: str = Form(...),
):
    """Post-header chip action: toggle +tag/-tag on the feed's tag_filter rule
    and apply it to unread entries immediately."""
    with get_meta_connection() as conn:
        result = toggle_feed_tag_filter(conn, feed_url, tag, sign)
    if "error" in result:
        return JSONResponse({"error": result["error"]}, status_code=400)
    result["ok"] = True
    return JSONResponse(result)


@router.post("/rules/run-now")
def rules_run_now_route(
    type: str = Form(...),
    scope: str = Form(...),
    scope_id: str = Form(""),
    keyword: str = Form(""),
    is_regex: int = Form(0),
    search_in: str = Form("title"),
    dedup_window_hours: int = Form(168),
    exclude_scope_ids: str = Form(""),
    fuzzy_pct: int = Form(_DEDUP_FUZZY_PCT_DEFAULT),
    min_title_words: int = Form(_DEDUP_MIN_TITLE_WORDS),
):
    with get_meta_connection() as conn:
        if type == "deduplicate":
            match_method = keyword if keyword in _DEDUP_VALID_MATCH_METHODS else "slug"
            # User-triggered Run Now sweeps the whole unread backlog. The default
            # 500-per-feed sample (right for post-refresh runs, where fresh dupes
            # are always in the newest slice) misses older duplicates entirely on
            # high-volume feeds — e.g. entries restored to unread days later.
            result = _run_now_dedup(
                conn,
                scope,
                scope_id,
                match_method,
                max(1, dedup_window_hours),
                max_per_feed=10000,
                exclude_scope_ids=exclude_scope_ids,
                fuzzy_threshold=_dedup_fuzzy_threshold(fuzzy_pct),
                min_title_words=_clamp_min_title_words(min_title_words),
            )
        elif type == "mark_as_read":
            result = _run_now_pattern(conn, scope, scope_id, keyword, bool(is_regex), search_in)
        elif type == "tag_filter":
            result = _run_tag_filter(conn, scope, scope_id, keyword)
        else:
            return JSONResponse({"error": f"Run Now not supported for type '{type}'"}, status_code=400)
    if "error" in result:
        return JSONResponse({"error": result["error"]}, status_code=400)
    with get_meta_connection() as conn:
        cur = conn.execute(
            "INSERT INTO rule_run_log (run_at, rule_type, scope, scope_id, keyword, entries_affected, trigger)"
            " VALUES (?, ?, ?, ?, ?, ?, 'manual')",
            (datetime.now().isoformat(), type, scope, scope_id, keyword, result.get("count", 0)),
        )
        log_id = cur.lastrowid
        log_rows = [(e, "marked") for e in (result.get("entries") or [])]
        log_rows += [(e, "kept") for e in (result.get("kept") or [])]
        if log_rows and log_id:
            conn.executemany(
                "INSERT INTO rule_run_log_entries"
                " (log_id, feed_url, entry_id, title, link, feed_title, role, matched_link)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (log_id, e["feed_url"], e["entry_id"], e["title"], e["link"], e["feed_title"], role, e.get("matched_link"))
                    for e, role in log_rows
                ],
            )
    result["ok"] = True
    return JSONResponse(result)


@router.get("/automation/history")
def automation_history_route(
    limit: int = Query(200),
    scope: str | None = Query(default=None),
    scope_id: str | None = Query(default=None),
    keyword: str | None = Query(default=None),
):
    filters: list[str] = []
    params: list[object] = []
    if scope is not None:
        filters.append("scope = ?")
        params.append(scope)
    if scope_id is not None:
        filters.append("scope_id = ?")
        params.append(scope_id)
    if keyword is not None:
        filters.append("keyword = ?")
        params.append(keyword)
    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    params.append(limit)
    with get_meta_connection() as conn:
        rows = conn.execute(
            f"SELECT id, run_at, rule_type, scope, scope_id, keyword, entries_affected, trigger"
            f" FROM rule_run_log {where} ORDER BY run_at DESC LIMIT ?",
            params,
        ).fetchall()
    return JSONResponse({"ok": True, "history": [dict(r) for r in rows]})


@router.get("/automation/history/{log_id}/entries")
def automation_history_entries_route(log_id: int):
    with get_meta_connection() as conn:
        rows = conn.execute(
            "SELECT feed_url, entry_id, title, link, feed_title, role, matched_link"
            " FROM rule_run_log_entries WHERE log_id = ? ORDER BY rowid",
            (log_id,),
        ).fetchall()
    return JSONResponse({"ok": True, "entries": [dict(r) for r in rows]})


@router.get("/dedup/false-matches")
def get_dedup_false_matches():
    with get_meta_connection() as conn:
        rows = conn.execute("SELECT keep_link, mark_link FROM dedup_false_matches ORDER BY added_at DESC").fetchall()
    return JSONResponse({"ok": True, "pairs": [{"keep_link": r[0], "mark_link": r[1]} for r in rows]})


@router.post("/dedup/false-match")
async def toggle_dedup_false_match(request: Request):
    data = await request.json()
    keep_link = str(data.get("keep_link") or "").strip()
    mark_link = str(data.get("mark_link") or "").strip()
    if not keep_link or not mark_link:
        return JSONResponse({"ok": False, "error": "keep_link and mark_link required"}, status_code=400)
    with get_meta_connection() as conn:
        existing = conn.execute(
            "SELECT 1 FROM dedup_false_matches WHERE keep_link = ? AND mark_link = ?",
            (keep_link, mark_link),
        ).fetchone()
        if existing:
            conn.execute(
                "DELETE FROM dedup_false_matches WHERE keep_link = ? AND mark_link = ?",
                (keep_link, mark_link),
            )
            active = False
        else:
            conn.execute(
                "INSERT INTO dedup_false_matches (keep_link, mark_link, added_at) VALUES (?, ?, ?)",
                (keep_link, mark_link, datetime.now().isoformat()),
            )
            active = True
    return JSONResponse({"ok": True, "active": active})
