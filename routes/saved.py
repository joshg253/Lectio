"""Saved-articles bulk-maintenance surface, plus the Save Article capture
routes: `/saved/folder/clear-curation`, the Saved-duplicate scan
(`/saved/duplicates*`, `/saved/deduplicate`), autofile
(`/saved/autofile*`), unstar-tagged (`/saved/unstar-tagged*`), archive-old
(`/saved/archive-old*`), the capture routes (`/articles/refresh-content`,
`/articles/save`), and the two scoped bulk actions
(`/saved/refetch-scope*`, `/saved/unstar-scope*`) -- 22 routes.

Stage 6 of the main.py route-by-URL-prefix split (Plan.md). No ordering
constraint: nothing here touches `_run_automation_after_refresh` or anything
else from the late `services.automation_rules` import, so this module is
imported alongside the plain `routes.compat_*`/`routes.tags`-style modules.

Plan.md's own note going in ("backed by services/saved_articles.py") only
half held up. `services/saved_articles.py` genuinely backs the capture path
(`save_article`/`refresh_captured_article`), but most of the *other* engines
in this cluster -- the cross-feed dupe scan, the autofile planner, the
unstar-tagged/archive-old planners, and the scoped batch re-fetch job -- are
main.py-resident logic with no service-layer home, and moved (or didn't)
based on whether main.py's own code and tests still need them, not on any
service boundary.

Several plan/engine helpers stay in main.py and are imported back rather
than moving with their only route caller, because a dedicated test exercises
them directly as `main.<name>` (the same "exercised directly by a dedicated
test file" precedent Stage 3 set for `get_highlight_keywords` and Stage 5
set for `_read_log_tail`): `_autofile_excluded_targets`
(tests/unit/test_autofile_excluded_targets.py), `_current_unstar_tagged_plan`
(tests/integration/test_unstar_tagged_route.py), `_saved_dup_host_slug`
(tests/unit/test_entry_dedupe_key.py), `_check_saved_url` and its own
`_looks_like_soft_404`/`_normalize_probe_path`/`_SOFT_404_PATH_NAMES`
neighborhood (tests/integration/test_saved_dedup.py,
tests/unit/test_soft_404_detection.py), and the whole scoped-refetch engine
-- `_refetch_job_state`, `_scope_refetchable`, `_refetch_begin`,
`_refetch_worker`, `_run_refetch_batch` (all exercised directly by
tests/integration/test_refetch_scope_batch.py /
test_refetch_scope_date_choice.py) -- which pulled its two untested siblings
`_refetch_scope_label`/`_refetch_status_payload` along with it rather than
splitting one tightly-coupled state machine across two files.
`clear_folder_curation` (tests/integration/test_clear_folder_curation.py)
stays for the same reason. `_scope_starred_keys` stays for the more usual
reason -- genuinely shared with a still-in-main.py Read Mode route, not just
tested directly.

Two more stay for a third reason -- not test coverage, but a real caller
outside this module, only found by grepping `routes/*.py` and `scripts/*.py`
too, not just main.py and tests: `_current_autofile_plan` (also called by
`routes/system.py`'s Instapaper import, to report what a fresh import could
auto-file) and `_saved_dup_groups`, plus its `_SAVED_DUP_BODY_HEAD_CHARS`/
`_SAVED_DUP_BODY_SQL_CHARS` constants (also called by
`scripts/measure_cross_feed_duplicates.py` and
`scripts/merge_saved_vs_real_duplicates.py` as `main.<name>`).

The Save Article capture helpers are the extreme case of "shared, so stays":
`_save_article_for_current_user`, `_refresh_captured_article_for_current_user`,
`_entry_source_url`, `_apply_mined_publish_date`, the whole auto-refetch-on-
keep machinery, and `fetch_full_page_article`/`fetch_readability_article`
all stay in main.py, because every one of them is also called from
`/api/save`, `/api/bookmarklet/save`, `/entries/saved`, `/entries/tags`, or
`/entries/autofetch-status` -- none of which moved here. Only the three
route handlers that are this module's own (`refresh_saved_article_content`,
`save_article_route`, `save_article_bookmarklet`) moved; nearly everything
they call stayed behind and is imported back.

`_saved_dup_reasons` (untested, single-caller) and the autofile/unstar-tagged/
archive-old *plan* builders except the two above that stayed
(`_bulk_reader_published_dates`, `_current_archive_old_stars_plan`) moved
with their routes, along with the constants only they use.

Two module-level service imports in main.py -- `saved_autofile_service` and
`archive_old_stars_service` -- lost their last main.py-resident caller once
this stage's routes moved, so `ruff --fix` tried to prune them as unused the
same way Step 2 of the `state.py` work had to `# noqa: F401` `_PerUserDict`;
both carry that same re-export marker now.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timezone
from urllib.parse import quote_plus

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from main import (
    _SAFE_DEDUP_MIN_BODY_CHARS,
    _SAFE_DEDUP_MIN_TITLE_WORDS,
    _SAFE_DEDUP_TAG_RE,
    _SAFE_DEDUP_UNCLOSED_TAG_RE,
    _SAVED_DUP_BODY_HEAD_CHARS,
    _SAVED_DUP_BODY_SQL_CHARS,
    CAPTURE_MODE_FULL,
    LOGGER,
    _autofile_excluded_targets,
    _check_saved_url,
    _current_autofile_plan,
    _current_unstar_tagged_plan,
    _entry_query_suffix,
    _hard_delete_entry,
    _move_entry_to_feed,
    _parse_stored_dt,
    _refetch_begin,
    _refetch_job_state,
    _refetch_scope_label,
    _refetch_status_payload,
    _refetch_worker,
    _refresh_captured_article_for_current_user,
    _run_in_user_context,
    _save_article_for_current_user,
    _saved_dup_groups,
    _saved_dup_host_slug,
    _scope_refetchable,
    _scope_starred_keys,
    apply_star_state,
    archive_old_stars_service,
    clear_folder_curation,
    fetch_full_page_article,
    get_archived_saved_keys,
    get_dedupe_host_aliases,
    get_meta_connection,
    get_reader,
    invalidate_unread_counts_cache,
    is_async_action_request,
    normalize_entry_link_for_dedupe,
    normalize_entry_title_for_dedupe,
    normalize_tag_value,
    refetch_batch,
    saved_articles_service,
    saved_autofile_service,
    tenancy,
)
from state import _refetch_jobs_lock

router = APIRouter()


@router.post("/saved/folder/clear-curation")
def clear_folder_curation_route(
    folder_id: int = Form(...),
    remove_stars: str = Form("0"),
    remove_tags: str = Form("0"),
    tag: str = Form(""),
):
    """Remove stars and/or manual tags from a folder's items in Saved.

    With *tag*, removes only that one tag (the "filter Saved by tag XYZ, remove
    XYZ from all shown" flow); without it, removes all manual tags. Either way
    non-destructive to Feeds: the folder and subscriptions are untouched, only
    curation is cleared, so items leave the Saved/Kept view. Deliberately *not*
    folder deletion, which unsubscribes feeds."""
    rs = remove_stars in ("1", "true", "on")
    rt = remove_tags in ("1", "true", "on")
    if not (rs or rt):
        return JSONResponse({"ok": False, "error": "Nothing selected to remove."}, status_code=400)
    result = clear_folder_curation(folder_id, rs, rt, only_tag=(tag.strip() or None))
    return JSONResponse({"ok": True, **result})


# ── Saved Articles duplicate scan ─────────────────────────────────────────────


def _saved_dup_reasons(group: list[dict], checks: list[tuple[str, str]]) -> list[str]:
    reasons = []
    for field, label in checks:
        vals = [r[field] for r in group if r[field]]
        if len(vals) != len(set(vals)):
            reasons.append(label)
    return reasons


@router.get("/saved/duplicates")
def get_saved_duplicates():
    """Report likely duplicate articles within Saved Articles (lectio:saved).

    Saved entries are keyed by their normalized URL, so re-saving the same URL
    can't duplicate — dupes are the same article saved under *different* URLs
    (amp/www/tracking-param variants, or an Instapaper import overlapping
    earlier saves). Two confidence tiers:

      confirmed — same canonical link or same URL slug.
      possible  — same normalized title, or same extracted-body prefix (catches
                  a re-save after the publisher fixed both the title and URL).

    Entries in each group are ordered keep-first (has extracted content, then
    oldest save). Read-only — the client posts the copies to remove to
    /saved/deduplicate.
    """
    import html as _html

    host_aliases = get_dedupe_host_aliases()
    saved_url = saved_articles_service.SAVED_FEED_URL
    with get_reader() as reader:
        if reader.get_feed(saved_url, None) is None:
            return JSONResponse({"confirmed": [], "possible": [], "scanned": 0})
        # Direct storage read: reader.get_entries() would materialize every
        # saved article's full extracted body; only a short prefix is needed.
        rows = (
            reader._storage.get_db()
            .execute(
                "SELECT id, link, title, published, read, substr(json_extract(content, '$[0].value'), 1, ?) FROM entries WHERE feed = ?",
                (_SAVED_DUP_BODY_SQL_CHARS, saved_url),
            )
            .fetchall()
        )

    records: list[dict] = []
    for entry_id, link, title, published, read, body_head in rows:
        link = str(link or entry_id)
        body = ""
        if body_head:
            body = _SAFE_DEDUP_TAG_RE.sub(" ", body_head)
            # The 2000-char SQL window can cut off inside a tag, leaving an
            # unclosed "<img …src="…long-url" fragment that _SAFE_DEDUP_TAG_RE
            # (which needs a closing >) can't strip. That fragment is the site
            # logo — byte-identical across every page on the site — so it made
            # unrelated articles (harrypotter.com/writing/*) false-match on
            # "same content". Drop a residual unclosed tag.
            body = _SAFE_DEDUP_UNCLOSED_TAG_RE.sub("", body)
            body = _html.unescape(body)
            body = " ".join(body.split())[:_SAVED_DUP_BODY_HEAD_CHARS].lower()
        ntitle = normalize_entry_title_for_dedupe(title)
        records.append(
            {
                "entry_id": str(entry_id),
                "link": link,
                "title": str(title or ""),
                "published": str(published or ""),
                "read": bool(read),
                "has_content": body_head is not None,
                "_canon": normalize_entry_link_for_dedupe(link, host_aliases),
                "_slug": _saved_dup_host_slug(link, host_aliases),
                "_ntitle": ntitle if len(ntitle.split()) >= _SAFE_DEDUP_MIN_TITLE_WORDS else "",
                "_body": body if len(body) >= _SAFE_DEDUP_MIN_BODY_CHARS else "",
            }
        )

    def _keep_order(r: dict):
        # Keeper first: prefer a copy with extracted content, then https over
        # http, then the oldest dated save (undated copies come from failed
        # extractions — keep last).
        return (not r["has_content"], not r["link"].startswith("https:"), not r["published"], r["published"])

    def _emit(groups: list[list[dict]], checks: list[tuple[str, str]]) -> list[dict]:
        for group in groups:
            group.sort(key=_keep_order)
        # Sort groups by their (already-keep-ordered) first entry's title, before
        # the "entries" reshape below loses the direct typing on that field.
        groups = sorted(groups, key=lambda group: str(group[0]["title"] or "").lower())
        out = []
        for group in groups:
            out.append(
                {
                    "reasons": _saved_dup_reasons(group, checks),
                    "entries": [{k: r[k] for k in ("entry_id", "link", "title", "published", "read", "has_content")} for r in group],
                }
            )
        return out

    confirmed_groups = _saved_dup_groups(records, ("_canon", "_slug"))
    confirmed_member: dict[str, int] = {}
    for gi, group in enumerate(confirmed_groups):
        for r in group:
            confirmed_member[r["entry_id"]] = gi

    possible_groups = []
    for group in _saved_dup_groups(records, ("_ntitle", "_body")):
        # Skip groups the confirmed tier already covers entirely (distinct
        # negative sentinels keep two non-members from looking like a match).
        gids = {confirmed_member.get(r["entry_id"], -1 - i) for i, r in enumerate(group)}
        if len(gids) == 1 and next(iter(gids)) >= 0:
            continue
        possible_groups.append(group)

    return JSONResponse(
        {
            "confirmed": _emit(confirmed_groups, [("_canon", "same URL"), ("_slug", "same slug")]),
            "possible": _emit(possible_groups, [("_ntitle", "same title"), ("_body", "same content")]),
            "scanned": len(records),
        }
    )


_SAVED_DUP_PREVIEW_CHARS = 1500
_SAVED_DUP_PREVIEW_MAX_IDS = 12


@router.post("/saved/duplicates/preview")
async def preview_saved_duplicates(request: Request):
    """Side-by-side compare support for the Saved duplicate scan: return a
    plain-text preview of each requested saved article's stored content so a
    'possible' match can be judged before anything is deleted.

    Body (JSON): {"entry_ids": [...]} — one group's ids (capped). Unknown ids
    are skipped."""
    import html as _html

    body = await request.json()
    entry_ids = [str(e) for e in body.get("entry_ids", []) if e][:_SAVED_DUP_PREVIEW_MAX_IDS]
    saved_url = saved_articles_service.SAVED_FEED_URL
    previews: list[dict] = []
    with get_reader() as reader:
        db = reader._storage.get_db()
        for entry_id in entry_ids:
            row = db.execute(
                "SELECT title, link, published, json_extract(content, '$[0].value') FROM entries WHERE feed = ? AND id = ?",
                (saved_url, entry_id),
            ).fetchone()
            if row is None:
                continue
            title, link, published, raw = row
            text = ""
            if raw:
                text = _SAFE_DEDUP_TAG_RE.sub(" ", raw)
                text = " ".join(_html.unescape(text).split())
            previews.append(
                {
                    "entry_id": entry_id,
                    "title": str(title or ""),
                    "link": str(link or entry_id),
                    "published": str(published or ""),
                    "chars": len(text),
                    "words": len(text.split()),
                    "text": text[:_SAVED_DUP_PREVIEW_CHARS],
                }
            )
    return JSONResponse({"previews": previews})


_SAVED_DUP_CHECK_PAUSE = 0.3  # between requests in one group — usually same host


@router.post("/saved/duplicates/check-urls")
async def check_saved_duplicate_urls(request: Request):
    """Probe one duplicate group's URLs for liveness (dead-link detection).

    Body (JSON): {"entry_ids": [...]} — one group's ids (capped). Sequential
    with a small pause (the copies usually share a host); the client is
    responsible for pacing across groups."""
    from starlette.concurrency import run_in_threadpool

    body = await request.json()
    entry_ids = [str(e) for e in body.get("entry_ids", []) if e][:_SAVED_DUP_PREVIEW_MAX_IDS]
    saved_url = saved_articles_service.SAVED_FEED_URL
    targets: list[tuple[str, str]] = []
    with get_reader() as reader:
        for entry_id in entry_ids:
            entry = reader.get_entry((saved_url, entry_id), None)
            if entry is not None:
                targets.append((entry_id, str(entry.link or entry_id)))

    def _probe_all() -> list[dict]:
        results = []
        for i, (entry_id, link) in enumerate(targets):
            if i:
                time.sleep(_SAVED_DUP_CHECK_PAUSE)
            results.append({"entry_id": entry_id, "link": link, **_check_saved_url(link)})
        return results

    return JSONResponse({"results": await run_in_threadpool(_probe_all)})


@router.post("/saved/deduplicate")
async def deduplicate_saved(request: Request):
    """Hard-delete the selected duplicate copies from Saved Articles.

    Body (JSON): {"entry_ids": [...]} — ids of the copies to remove (a saved
    entry's id is its normalized article URL). Each one is tombstoned and
    deleted exactly like /entries/delete."""
    body = await request.json()
    entry_ids = [str(e) for e in body.get("entry_ids", []) if e]
    saved_url = saved_articles_service.SAVED_FEED_URL
    deleted = 0
    errors = 0
    with get_reader() as reader:
        for entry_id in entry_ids:
            entry = reader.get_entry((saved_url, entry_id), None)
            if entry is None:
                continue
            try:
                _hard_delete_entry(reader, saved_url, entry_id, entry)
                deleted += 1
            except Exception:  # noqa: BLE001
                LOGGER.exception("[saved-dedup] delete failed for %s", entry_id)
                errors += 1
    if deleted:
        invalidate_unread_counts_cache()
    return JSONResponse({"ok": errors == 0, "deleted": deleted, "errors": errors})


# Filing runs at roughly 17 articles/second, so ~150 keeps a call near ten
# seconds — short enough to survive a reverse proxy and to report progress.
_AUTOFILE_BATCH = 150
_AUTOFILE_BATCH_MAX = 500


@router.get("/saved/autofile/preview")
def preview_saved_autofile():
    """Propose a home feed for each unfiled saved article, grouped by host.

    Read-only. Saved articles imported from a read-later service are mostly
    articles from feeds already subscribed to, so they can be filed onto their
    real feed — which also collapses cross-feed duplicates, since the move
    matches into the target by GUID else normalized link.

    The client reviews and approves per host; see services/saved_autofile for
    why a lone candidate feed isn't automatically a trustworthy one.
    """
    plan, marked, _titles = _current_autofile_plan()
    # entry_ids are only needed server-side on apply; sending 4k of them per
    # host would bloat the preview for no benefit.
    slim = [{k: v for k, v in c.items() if k != "entry_ids"} for c in plan]
    totals = saved_autofile_service.plan_totals(plan)
    totals["non_feed_hosts"] = len(marked)
    totals["non_feed_articles"] = sum(c["count"] for c in marked)
    return JSONResponse(
        {
            "plan": slim,
            "totals": totals,
            "non_feed": sorted(
                ({"host": c["host"], "count": c["count"]} for c in marked),
                key=lambda c: (-c["count"], c["host"]),
            ),
        }
    )


@router.get("/saved/unstar-tagged/preview")
def preview_unstar_tagged(keep_tags: str = Query("")):
    """Preview which starred+tagged entries would be unstarred.

    Read-only. After tag-as-keep a tag already keeps an entry, so a star on a
    tagged entry is redundant clutter in the read-later queue. *keep_tags* is a
    comma-separated opt-out; any entry carrying one of those tags is protected.

    The per-tag breakdown and the suggested queue-like opt-outs let the reviewer
    keep aspirational reading queues (`games-to-play`, `books`) starred while
    clearing topical filing tags. The entry-id lists aren't sent — the preview
    only needs counts, and apply recomputes the set under the same keep_tags.
    """
    keep = {t.strip() for t in keep_tags.split(",") if t.strip()}
    plan = _current_unstar_tagged_plan(keep)
    return JSONResponse(
        {
            "totals": plan["totals"],
            "per_tag": plan["per_tag"],
            "queue_like_tags": plan["queue_like_tags"],
        }
    )


@router.post("/saved/unstar-tagged")
async def apply_unstar_tagged(request: Request):
    """Unstar every starred entry that carries a manual tag, minus opt-outs.

    Body (JSON): {"keep_tags": [...]}. Recomputes the plan server-side under the
    given opt-outs rather than trusting a client-supplied id list — the preview
    is advisory, the decision is made here against live data.

    Only the star row is deleted. Manual tags, read state, and the offline
    archive are untouched: a tagged entry keeps its capture (the unstar route's
    archive-removal is gated on having no tags, and this bypasses that path
    entirely, so nothing is ever enqueued for removal).
    """
    body = await request.json()
    keep = {str(t).strip() for t in body.get("keep_tags", []) if str(t).strip()}
    plan = _current_unstar_tagged_plan(keep)
    to_unstar = plan["to_unstar"]
    if not to_unstar:
        return JSONResponse({"ok": True, "unstarred": 0})

    deleted = 0
    with get_meta_connection() as conn:
        for start in range(0, len(to_unstar), 400):
            chunk = to_unstar[start : start + 400]
            placeholders = ",".join("(?,?)" for _ in chunk)
            flat = [v for key in chunk for v in key]
            cur = conn.execute(
                f"DELETE FROM saved_entries WHERE (feed_url, entry_id) IN ({placeholders})",
                flat,
            )
            deleted += cur.rowcount
        conn.commit()

    # A behind-the-back delete leaves the generation-guarded counts stale.
    invalidate_unread_counts_cache()
    return JSONResponse(
        {
            "ok": True,
            "unstarred": deleted,
        }
    )


def _bulk_reader_published_dates(keys: list[tuple[str, str]]) -> dict[tuple[str, str], datetime | None]:
    """(feed, id) -> published, read directly from reader's entries table.

    Mirrors the existing raw-connection reads in this module (get_tagged_entry_keys,
    _sorted_star_key_window) — reader's high-level API has no bulk-by-key lookup."""
    out: dict[tuple[str, str], datetime | None] = {}
    if not keys:
        return out
    conn = sqlite3.connect(str(tenancy.reader_db_path()), timeout=5.0)
    try:
        for start in range(0, len(keys), 400):
            chunk = keys[start : start + 400]
            placeholders = ",".join("(?,?)" for _ in chunk)
            params = [v for k in chunk for v in k]
            for feed, eid, published in conn.execute(
                f"SELECT feed, id, published FROM entries WHERE (feed, id) IN ({placeholders})",
                params,
            ):
                out[(str(feed), str(eid))] = _parse_stored_dt(published)
    finally:
        conn.close()
    return out


def _current_archive_old_stars_plan(days: int, basis: str = "published") -> dict:
    """Assemble the archive-old-stars plan for the current user. Read-only.

    basis="published" (default): the article's own publish date.
    basis="saved": saved_entries.saved_at — offered, but unreliable for most
    rows. The 2026-06 multi-user migration stamped its own run date over
    years-old Inoreader stars: 6,091 of 10,002 rows carry a saved_at in that
    one week, only 419 are a genuine Lectio-made star. A 30-day cutoff would
    sweep those 6,091 in and a 90-day cutoff would protect them, neither for
    any real reason — hence "published" is the default, not "saved"."""
    basis = basis if basis in ("published", "saved") else "published"
    with get_meta_connection() as conn:
        rows = conn.execute("SELECT feed_url, entry_id, saved_at FROM saved_entries").fetchall()
    keys = [(str(f), str(e)) for f, e, _s in rows]
    if basis == "saved":
        starred_at: dict[tuple[str, str], datetime | None] = {(str(f), str(e)): _parse_stored_dt(s) for f, e, s in rows}
    else:
        starred_at = _bulk_reader_published_dates(keys)
    plan = archive_old_stars_service.build_archive_plan(
        starred_at,
        get_archived_saved_keys(),
        days=days,
    )
    plan["basis"] = basis
    return plan


@router.get("/saved/archive-old/preview")
def preview_archive_old_stars(
    days: int = Query(archive_old_stars_service.DEFAULT_DAYS),
    basis: str = Query("published"),
):
    """Preview which stars would be archived. Changes nothing."""
    plan = _current_archive_old_stars_plan(days, basis)
    return JSONResponse(
        {
            "ok": True,
            "days": plan["days"],
            "basis": plan["basis"],
            "cutoff": plan["cutoff"],
            "totals": plan["totals"],
            "buckets": plan["buckets"],
            "day_choices": list(archive_old_stars_service.DAY_CHOICES),
        }
    )


@router.post("/saved/archive-old")
async def apply_archive_old_stars(request: Request):
    """Archive every star older than N days — the Inbox bankruptcy pass.

    Recomputes the plan server-side from the given ``days`` rather than trusting a
    client id list, the same as the unstar-tagged apply.

    **Reversible per item, and nothing is lost**: the tag, the offline capture and
    pruning-exemption all survive, because archived is itself a keep signal. This
    is why it is the right instrument for the Inbox backlog and #5's unstar sweep
    is not — see services/archive_old_stars.py.

    Written in bulk rather than by looping the single-entry route, for two
    reasons beyond speed:

    - **It must not write ``read_history``.** That table is capped at 2,000 rows
      and is the only reverse-chronological record of what has been dealt with —
      the thing that made dropping a separate Archive view acceptable. Pushing
      9,000 bulk archives through it would evict the entire real history.
    - **No capture-release check is needed.** The archived rows are written first,
      so every entry provably still carries a keep signal; the per-entry
      ``entry_has_keep_signal`` probe would be two queries each to answer "yes".
    """
    body = await request.json()
    try:
        days = int(body.get("days", archive_old_stars_service.DEFAULT_DAYS))
    except TypeError, ValueError:
        return JSONResponse({"ok": False, "error": "days must be a number"}, status_code=400)
    basis = str(body.get("basis", "published"))

    plan = _current_archive_old_stars_plan(days, basis)
    keys = plan["to_archive"]
    if not keys:
        return JSONResponse({"ok": True, "archived": 0, "days": days, "basis": plan["basis"]})

    now_iso = datetime.now(timezone.utc).isoformat()
    with get_meta_connection() as conn:
        conn.execute("PRAGMA busy_timeout = 20000")
        for start in range(0, len(keys), 400):
            chunk = keys[start : start + 400]
            conn.executemany(
                "INSERT OR IGNORE INTO archived_entries (feed_url, entry_id, archived_at) VALUES (?, ?, ?)",
                [(f, e, now_iso) for f, e in chunk],
            )
            # The star comes off: the TODO is discharged.
            placeholders = ",".join("(?,?)" for _ in chunk)
            conn.execute(
                f"DELETE FROM saved_entries WHERE (feed_url, entry_id) IN ({placeholders})",
                [v for k in chunk for v in k],
            )
            # Read at the override level too, so a refresh can't un-read them.
            conn.executemany(
                "INSERT OR REPLACE INTO entry_read_state (feed_url, entry_id, read_at) VALUES (?, ?, ?)",
                [(f, e, now_iso) for f, e in chunk],
            )
        conn.commit()

    marked_read = 0
    try:
        with get_reader() as reader:
            for feed_url, entry_id in keys:
                try:
                    reader.mark_entry_as_read((feed_url, entry_id))
                    marked_read += 1
                except Exception:  # noqa: BLE001 — a missing entry is not fatal here
                    pass
    except Exception:
        LOGGER.warning("archive-old-stars: reader mark-read pass failed", exc_info=True)

    # A behind-the-back write leaves the generation-guarded counts stale.
    invalidate_unread_counts_cache()
    LOGGER.info(
        "[archive-old-stars] archived %d star(s) older than %dd by %s (marked read: %d)", len(keys), days, plan["basis"], marked_read
    )
    return JSONResponse(
        {
            "ok": True,
            "archived": len(keys),
            "days": days,
            "basis": plan["basis"],
            "marked_read": marked_read,
        }
    )


@router.post("/saved/autofile/non-feed-subscription")
async def mark_non_feed_subscription(request: Request):
    """Mark or unmark a *subscription* as not really a feed.

    Body (JSON): {"feed_urls": [...], "marked": bool}. Some subscriptions are a
    single article URL that got added as a feed — they sit on exactly the right
    host, so they look like the site's feed and would collect that site's whole
    backlog. The subscription is left alone (its entries are real reading); it
    is only barred as a filing destination.
    """
    body = await request.json()
    feed_urls = [str(u).strip() for u in body.get("feed_urls", []) if u]
    if not feed_urls:
        return JSONResponse({"ok": False, "error": "No feeds given."}, status_code=400)
    marked = bool(body.get("marked", True))
    with get_meta_connection() as conn:
        if marked:
            conn.executemany(
                "INSERT OR IGNORE INTO non_feed_subscriptions (feed_url) VALUES (?)",
                [(u,) for u in feed_urls],
            )
        else:
            conn.executemany(
                "DELETE FROM non_feed_subscriptions WHERE feed_url = ?",
                [(u,) for u in feed_urls],
            )
        conn.commit()
    return JSONResponse({"ok": True, "feed_urls": feed_urls, "marked": marked})


@router.post("/saved/autofile/non-feed")
async def mark_autofile_non_feed(request: Request):
    """Mark or unmark hosts as never having a feed.

    Body (JSON): {"hosts": [...], "marked": bool}. Purely a worklist decision —
    the saved articles themselves are untouched and stay exactly as they are.
    They were never unfiled feed articles in the first place; they're one-off
    read-later captures (a cheat sheet, a single tutorial), and the auto-filer
    kept re-proposing them because it can only see that no feed matches.
    """
    body = await request.json()
    hosts = [saved_autofile_service.article_host(h) or str(h).strip().lower() for h in body.get("hosts", []) if h]
    hosts = [h for h in hosts if h]
    if not hosts:
        return JSONResponse({"ok": False, "error": "No hosts given."}, status_code=400)
    marked = bool(body.get("marked", True))
    with get_meta_connection() as conn:
        if marked:
            conn.executemany(
                "INSERT OR IGNORE INTO autofile_non_feed_hosts (host) VALUES (?)",
                [(h,) for h in hosts],
            )
        else:
            conn.executemany("DELETE FROM autofile_non_feed_hosts WHERE host = ?", [(h,) for h in hosts])
        conn.commit()
    return JSONResponse({"ok": True, "hosts": hosts, "marked": marked})


@router.post("/saved/autofile")
async def apply_saved_autofile(request: Request):
    """File the approved hosts' saved articles onto their target feeds.

    Body (JSON): {"hosts": [{"host": ..., "target_feed_url": ...}, ...],
                  "limit": int} — the target is taken from the request, not
    recomputed, so what the user approved in the preview is exactly what runs.
    Each article goes through _move_entry_to_feed, which migrates star/tags/read
    state and then removes the now-empty saved source.

    *limit* caps how many articles one call moves, and the response reports what
    is still outstanding so the client can loop. Filing is ~17 articles/second,
    so an uncapped run over a big host (1,300+ articles) takes well over a
    minute and gets cut off by a proxy or the browser mid-flight — the work
    lands but the caller never learns it did, and the list looks untouched.
    """
    body = await request.json()
    wanted = {
        str(h.get("host") or ""): str(h.get("target_feed_url") or "")
        for h in body.get("hosts", [])
        if h.get("host") and h.get("target_feed_url")
    }
    if not wanted:
        return JSONResponse({"ok": False, "error": "No hosts selected."}, status_code=400)

    try:
        limit = int(body.get("limit") or _AUTOFILE_BATCH)
    except TypeError, ValueError:
        limit = _AUTOFILE_BATCH
    limit = max(1, min(limit, _AUTOFILE_BATCH_MAX))

    saved_url = saved_articles_service.SAVED_FEED_URL
    moved = 0
    failed = 0
    remaining = 0
    per_host: dict[str, int] = {}
    with get_reader() as reader, get_meta_connection() as conn:
        known_feeds = {str(f.url) for f in reader.get_feeds()}
        bad = sorted(set(wanted.values()) - known_feeds)
        if bad:
            return JSONResponse({"ok": False, "error": f"Unknown target feed: {bad[0]}"}, status_code=400)
        # Enforced here too, not just in the preview: the target comes from the
        # request, so a stale plan must not be able to file into a barred feed.
        barred = sorted(set(wanted.values()) & _autofile_excluded_targets(known_feeds, conn))
        if barred:
            return JSONResponse(
                {"ok": False, "error": f"Not a valid target feed: {barred[0]}"},
                status_code=400,
            )
        rows = reader._storage.get_db().execute("SELECT id, link FROM entries WHERE feed = ?", (saved_url,)).fetchall()
        kept = {r[0] for r in conn.execute("SELECT entry_id FROM saved_entries WHERE feed_url = ?", (saved_url,))}
        for entry_id, link in rows:
            entry_id = str(entry_id)
            if entry_id not in kept:
                continue
            host = saved_autofile_service.article_host(str(link or entry_id))
            target = wanted.get(host)
            if not target:
                continue
            if moved >= limit:
                remaining += 1  # counted, not moved — the client loops
                continue
            res = _move_entry_to_feed(reader, conn, saved_url, entry_id, target)
            if res.get("ok"):
                moved += 1
                per_host[host] = per_host.get(host, 0) + 1
            else:
                failed += 1
                LOGGER.warning("[autofile] %s -> %s failed: %s", entry_id, target, res.get("error"))
    if moved:
        invalidate_unread_counts_cache()
    LOGGER.info("[autofile] filed %d saved article(s) across %d host(s), %d failed, %d left", moved, len(per_host), failed, remaining)
    return JSONResponse({"ok": failed == 0, "moved": moved, "failed": failed, "remaining": remaining, "per_host": per_host})


@router.post("/articles/refresh-content")
async def refresh_saved_article_content(
    request: Request,
    feed_url: str = Form(...),
    entry_id: str = Form(...),
    mode: str = Form("readability"),
    date_choice: str = Form(""),
):
    """Re-fetch + re-extract a captured article's content, replacing the stored
    copy and bumping it to the top. Fixes a bad initial capture (e.g. readability
    grabbed a fragment, or a broken import) without deleting and re-adding.

    *date_choice*, one of "now"/"original"/"pub" (blank = today's default:
    a capture bumps, a feed entry doesn't) — see
    saved_articles_service.refresh_captured_article.

    Works for any Lectio capture, wherever it lives, and always re-fetches the
    entry's current **link** rather than its id. Two bugs made that necessary:

    - Gating on feed identity stripped the hatch from every article auto-filing
      moved out of `lectio:saved` (~3,900 of them).
    - Re-fetching by entry id ignored an **Edit URL** correction entirely: a
      capture's id is the address it was first saved from and never changes, so
      a repointed article kept re-fetching the dead URL and reporting success.

    *mode* ``"full"`` re-captures the whole page instead of readability-
    extracting — for a page readability mangles (prose scattered across
    low-scoring divs, or a lead image dropped by its cleaning). The save-path
    fallback below is readability-only; a full-page re-capture needs an entry to
    already exist, which after any real capture it does.

    The save path is kept only as a fallback for the case it is actually good
    at — a saved URL with no entry behind it yet."""
    _dc = date_choice if date_choice in {"now", "original", "pub"} else None
    # positional: feed_url, entry_id, mode, bump_received (unused here — the
    # date_choice picker is the only knob this route exposes), date_choice.
    # ignore_cooldown=True: a person clicking "Refetch content" is asking on
    # purpose, not the polite background traffic the page-fetch ladder's own
    # host cooldown exists to pace.
    result = await run_in_threadpool(
        _refresh_captured_article_for_current_user,
        feed_url,
        entry_id,
        mode,
        None,
        _dc,
        ignore_cooldown=True,
    )
    if result.get("ok"):
        return JSONResponse(
            {
                "ok": True,
                "refreshed": bool(result.get("refreshed")),
                "extracted": bool(result.get("extracted")),
                "title": result.get("title"),
                "feed_url": feed_url,
                "entry_id": entry_id,
                "url": result.get("source_url") or entry_id,
                "dated": result.get("dated"),
                "from_archive": result.get("from_archive"),
            }
        )
    # Only the saved feed has a meaningful fallback: re-running the save path
    # can create the entry when it is genuinely absent. Anywhere else, the
    # in-place result is the answer.
    if not saved_articles_service.is_saved_articles_feed(feed_url):
        return JSONResponse(
            {"ok": False, "error": result.get("error") or "Re-fetch failed.", "dead": bool(result.get("dead"))},
            status_code=400,
        )
    url = saved_articles_service.normalize_article_url(entry_id) or entry_id
    result = await run_in_threadpool(_save_article_for_current_user, url, None, True)
    # save_article treats extraction failure as non-fatal — right when *saving*
    # (the bookmark is still worth keeping and the archive worker can retry),
    # but wrong here: a re-fetch that extracted nothing changed nothing, and
    # reporting ok made the button look like it worked while the article sat
    # untouched (treblezine, reported 2026-07-26 as "refetches nothing but no
    # error"). Only a real extraction counts as a successful re-fetch.
    if not result.get("ok") or not result.get("extracted"):
        return JSONResponse(
            {"ok": False, "error": result.get("error") or "Re-fetch got nothing back from the page.", "dead": bool(result.get("dead"))},
            status_code=400,
        )
    return JSONResponse(
        {
            "ok": True,
            "refreshed": bool(result.get("refreshed")),
            "extracted": bool(result.get("extracted")),
            "title": result.get("title"),
            "feed_url": feed_url,
            "entry_id": entry_id,  # stored key
            "url": url,  # normalized source URL that was re-fetched
        }
    )


@router.post("/articles/save")
def save_article_route(request: Request, url: str = Form(...), mode: str = Form("readability")):
    """In-app save (Save Article modal). Session-authenticated.

    *mode* ``"full"`` captures the whole page body instead of readability-
    extracting it, for a document-shaped page readability mangles — the same
    escape hatch `/articles/refresh-content` offers after the fact, offered at
    save time so a known-bad shape doesn't have to be captured wrong first.
    Deliberately not the default: on a blog-shaped page it keeps the nav and
    sidebar chrome readability would strip."""
    extract = fetch_full_page_article if mode == CAPTURE_MODE_FULL else None
    result = _save_article_for_current_user(url, extract)
    if is_async_action_request(request, "lectio-save-article"):
        status = 200 if result["ok"] else 400
        return JSONResponse(result, status_code=status)
    if not result["ok"]:
        return RedirectResponse(
            url="/?message=" + quote_plus(result["error"] or "Could not save the article."),
            status_code=303,
        )
    entry_query = _entry_query_suffix(result["feed_url"], result["entry_id"])
    return RedirectResponse(
        url=f"/?list_feed_url={quote_plus(result['feed_url'])}{entry_query}",
        status_code=303,
    )


@router.get("/articles/save")
def save_article_bookmarklet(request: Request, url: str = Query(...)):
    """Bookmarklet save: a top-level GET navigation, so the session cookie
    rides along (SameSite=Lax) and an unauthenticated hit lands on /login with
    a ?next= that finishes the save after sign-in."""
    result = _save_article_for_current_user(url)
    if not result["ok"]:
        return RedirectResponse(
            url="/?message=" + quote_plus(result["error"] or "Could not save the article."),
            status_code=303,
        )
    message = "Article already saved." if result["duplicate"] else "Article saved."
    entry_query = _entry_query_suffix(result["feed_url"], result["entry_id"])
    return RedirectResponse(
        url=(f"/?list_feed_url={quote_plus(result['feed_url'])}{entry_query}&message={quote_plus(message)}"),
        status_code=303,
    )


@router.get("/saved/refetch-scope/preview")
def preview_refetch_scope(
    folder_id: int | None = Query(default=None),
    list_feed_url: str | None = Query(default=None),
):
    """How many articles a batch re-fetch would touch, and how long it would take."""
    rows = _scope_refetchable(folder_id, list_feed_url)
    job = _refetch_job_state()
    return JSONResponse(
        {
            "ok": True,
            "count": len(rows),
            "hosts": len({refetch_batch.host_of(r[2]) for r in rows}),
            "estimate_seconds": int(refetch_batch.estimate_seconds(rows)),
            # So the confirm can say "this will be queued behind N" rather than the
            # caller discovering it only after committing.
            "busy": bool(job and job.get("running")),
            "queued": len(job.get("queue") or []) if job else 0,
        }
    )


@router.get("/saved/refetch-scope/status")
def refetch_scope_status():
    """Progress of the running batch, what is queued behind it, and recent runs.

    Polled by the status pill, which is the only place a background job that runs
    for a quarter of an hour is actually visible.
    """
    return JSONResponse(_refetch_status_payload(_refetch_job_state()))


@router.post("/saved/refetch-scope/cancel")
async def cancel_refetch_scope(request: Request):
    """Stop the current run; optionally drop what is queued behind it too."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — a bare cancel needs no body
        body = {}
    job = _refetch_job_state()
    if not job:
        return JSONResponse({"ok": True, "cancelling": False})
    if body.get("queued_index") is not None:
        # Drop one queued scope without touching the run in flight.
        idx = int(body["queued_index"])
        queue = job.get("queue") or []
        if 0 <= idx < len(queue):
            queue.pop(idx)
        return JSONResponse({"ok": True, "dropped": True})
    if body.get("all"):
        job["cancel_all"] = True
        (job.get("queue") or []).clear()
    if job.get("running"):
        job["cancel"] = True
        return JSONResponse({"ok": True, "cancelling": True})
    return JSONResponse({"ok": True, "cancelling": False})


@router.post("/saved/refetch-scope")
async def start_refetch_scope(request: Request):
    """Start a paced batch re-fetch over a folder or feed, or queue one behind the
    run in flight.

    A background thread rather than a request: a hundred articles at ten seconds a
    host is a quarter of an hour, which no request should hold open. Progress is
    polled from /status.

    **Queued, not refused.** Only one batch may run at a time — two overlapping
    runs would each honor the pacing and together double the rate every site sees
    — but that is a reason to serialize them, not to make the user wait at the
    keyboard and remember to come back. Queued scopes are resolved to entries when
    they start, not when they are queued, so a queue that waits an hour still acts
    on what is kept *now*.

    Bulk is only reasonable because every protection the single re-fetch has
    applies per entry — the guard refuses a wrong page rather than overwriting, the
    previous body is snapshotted so any result is revertible, a refusal falls back
    to the archive, and a missing publish date is learned on the way.
    """
    body = await request.json()
    folder_id = body.get("folder_id")
    list_feed_url = body.get("list_feed_url") or None
    if folder_id is None and not list_feed_url:
        return JSONResponse({"ok": False, "error": "Pick a feed or a folder."}, status_code=400)
    folder_id = int(folder_id) if folder_id is not None else None
    # Same "now"/"original"/"pub" picker as the single-article re-fetch (see
    # refresh_content_route) — applied per article, so "original"/"pub" land
    # each one on its own date, only "now" is uniform across the batch.
    date_choice = body.get("date_choice")
    date_choice = date_choice if date_choice in {"now", "original", "pub"} else None

    rows = _scope_refetchable(folder_id, list_feed_url)
    if not rows:
        return JSONResponse({"ok": False, "error": "Nothing kept here to re-fetch."}, status_code=400)
    label = _refetch_scope_label(folder_id, list_feed_url)
    estimate = int(refetch_batch.estimate_seconds(rows))

    job = _refetch_job_state(create=True)
    with _refetch_jobs_lock:
        if job.get("running"):
            queue = job.setdefault("queue", [])
            if any(q["folder_id"] == folder_id and q["list_feed_url"] == list_feed_url for q in queue):
                return JSONResponse({"ok": False, "error": f"{label} is already queued."}, status_code=409)
            queue.append(
                {
                    "folder_id": folder_id,
                    "list_feed_url": list_feed_url,
                    "label": label,
                    "count": len(rows),
                    "estimate_seconds": estimate,
                    "date_choice": date_choice,
                }
            )
            return JSONResponse(
                {"ok": True, "queued": True, "position": len(queue), "total": len(rows), "estimate_seconds": estimate, "label": label}
            )
        _refetch_begin(job, label, rows, estimate, date_choice=date_choice)

    uid = tenancy.current_user_id()
    threading.Thread(
        target=lambda: _run_in_user_context(uid, _refetch_worker, rows, job),
        daemon=True,
    ).start()
    return JSONResponse({"ok": True, "started": True, "total": len(rows), "estimate_seconds": estimate, "label": label})


@router.get("/saved/unstar-scope/preview")
def preview_unstar_scope(
    folder_id: int | None = Query(default=None),
    list_feed_url: str | None = Query(default=None),
    tag: str | None = Query(default=None),
):
    """How many stars the current view holds. Changes nothing.

    The count comes from the server so the button can state the exact number
    before it is pressed — the rule every bulk action here follows, because a
    number the client guessed is a number the action does not honor.
    """
    keys = _scope_starred_keys(folder_id, list_feed_url, tag)
    return JSONResponse({"ok": True, "count": len(keys), "tag": normalize_tag_value(tag), "feed_url": list_feed_url})


@router.post("/saved/unstar-scope")
def apply_unstar_scope(
    request: Request,
    folder_id: int | None = Form(default=None),
    list_feed_url: str | None = Form(default=None),
    tag: str | None = Form(default=None),
):
    """Remove every star in the drilled-down view.

    Recomputes the set server-side from the scope rather than trusting an id list,
    matching the other bulk actions.

    Goes through ``apply_star_state`` per entry rather than one bulk DELETE. That
    is not fastidiousness: the unstar path releases the offline capture and
    hard-deletes a `lectio:saved` husk once no keep signal remains, and a bulk
    DELETE would skip both — leaving orphaned captures and invisible husks behind.
    Tags are untouched; dropping a tag is *Delete tag everywhere*.
    """
    keys = _scope_starred_keys(folder_id, list_feed_url, tag)
    for feed_u, entry_id in keys:
        try:
            apply_star_state(feed_u, entry_id, False)
        except Exception:  # noqa: BLE001 — one bad entry must not abort the sweep
            LOGGER.warning("unstar-scope failed for %s/%s", feed_u, entry_id, exc_info=True)
    invalidate_unread_counts_cache()
    LOGGER.info("[unstar-scope] removed %d star(s) (feed=%s tag=%s folder=%s)", len(keys), list_feed_url, tag, folder_id)
    if is_async_action_request(request, "lectio-ajax"):
        return JSONResponse({"ok": True, "unstarred": len(keys)})
    return RedirectResponse(url=request.headers.get("referer") or "/read", status_code=303)
