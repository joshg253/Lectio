"""Post-refresh automation rule execution: run-log writer, entry excerpting,
keyword-rule matching, the three user-triggerable "run now" rule executors
(deduplicate/mark_as_read/tag_filter), and the six after-refresh dispatchers
(email/webhook/instapaper/quire/save_article/youtube_playlist) that
_run_automation_after_refresh (still in main.py) calls after every scheduled
refresh.

Extracted out of main.py (Plan.md's main.py/index.html breakup, Step 2,
Stages B-D). This module does `from main import ...` for a handful of
main.py-resident primitives (dedup/pattern helpers staying in main.py -- see
Plan.md's Step 4, `services/dedup.py` -- credential/action helpers like
`_quire_add_entry`/`_instapaper_save_url`, and scope/setting helpers with no
other home yet) -- it is imported late, from main.py's own bottom-of-file
section, for the same reason the routes/integrations_*.py modules are: those
names don't exist yet earlier in main.py's execution. See routes/__init__.py's
docstring for the full mechanics and the import-order gotcha it creates for
anything that imports this module directly before main has finished loading.

Landmine: email_article (immediate) and webhook deliveries have no
idempotency guard beyond the 15-minute `added` cutoff -- quire is likewise
unguarded (rate-limited, not deduped). instapaper/save_article/
youtube_playlist are all safe (URL/duplicate/INSERT-OR-IGNORE guarded). Don't
ever leave two live copies of one of the unguarded three reachable at once.
"""

from __future__ import annotations

import collections
import re
import sqlite3
from datetime import datetime

from main import (
    _DEDUP_MIN_TITLE_WORDS,
    LOGGER,
    PROFILE_EMAIL_SETTING_KEY,
    SETTING_INSTAPAPER_PASSWORD,
    SETTING_INSTAPAPER_USERNAME,
    SETTING_YT_PLAYLIST_AUTO_LAST_CHECK,
    _bump_unread_counts_generation,
    _flush_email_batch_for_rule,
    _instapaper_save_url,
    _is_youtube_short,
    _quire_add_entry,
    _resolve_dedup_feed_urls,
    _safe_dedup_collect,
    _safe_dedup_find_pairs,
    _star_entry_for_current_user,
    author_filter_token,
    build_keyword_matcher,
    dedup_order_key,
    entry_effective_date,
    entry_url_slug,
    feed_display_title,
    feed_in_rule_scope,
    get_folder_feed_urls,
    get_highlight_keywords,
    get_manual_tags_for_entry,
    get_meta_connection,
    get_quire_usage_status,
    get_quire_user_token,
    get_reader,
    get_resend_api_key,
    get_resend_from,
    get_runtime_setting,
    get_setting,
    get_youtube_oauth_token,
    is_email_configured,
    is_quire_configured,
    mark_yt_quota_exhausted,
    normalize_entry_title_for_dedupe,
    normalize_tag_value,
    parse_folders_scope_id,
    parse_tag_filter_spec,
    quire_project_oid,
    resolve_rule_feed_urls,
    rule_scope_folder_feed_set,
    rule_scope_folder_ids,
    set_setting,
    title_word_similarity,
    youtube_duration_service,
)
from services import html_sanitize, youtube_embeds
from services import youtube_oauth as youtube_oauth_service
from services.email import send_article_email
from services.webhooks import build_webhook_batch_payload, build_webhook_payload, send_webhook


def _log_auto_run(
    conn: sqlite3.Connection, now: str, rule_type: str, scope: str, scope_id: str, keyword: str, result: dict, trigger: str = "auto"
) -> None:
    """Write a rule_run_log row (+ matched entries) in the caller's transaction."""
    cur = conn.execute(
        "INSERT INTO rule_run_log (run_at, rule_type, scope, scope_id, keyword, entries_affected, trigger) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (now, rule_type, scope, scope_id, keyword, result["count"], trigger),
    )
    rows = [(e, "marked") for e in (result.get("entries") or [])]
    rows += [(e, "kept") for e in (result.get("kept") or [])]
    if rows and cur.lastrowid:
        conn.executemany(
            "INSERT INTO rule_run_log_entries"
            " (log_id, feed_url, entry_id, title, link, feed_title, role, matched_link)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (cur.lastrowid, e["feed_url"], e["entry_id"], e["title"], e["link"], e["feed_title"], role, e.get("matched_link"))
                for e, role in rows
            ],
        )


def _get_entry_excerpt(entry: object) -> str:
    """Return a short plain-text excerpt from an entry's content or summary."""
    raw = ""
    content = getattr(entry, "content", None) or []
    for c in content:
        val = getattr(c, "value", None) or ""
        if val:
            raw = val
            break
    if not raw:
        raw = str(getattr(entry, "summary", None) or "")
    return html_sanitize.plain_text_excerpt(raw)


def _entry_matches_rule(entry: object, keyword: str, is_regex: bool, search_in: str) -> bool:
    if not keyword:
        return False
    try:
        match_fn = build_keyword_matcher(keyword, is_regex)
    except re.error:
        return False

    title = str(getattr(entry, "title", None) or "")
    body = ""
    if search_in in ("body", "both"):
        for c in getattr(entry, "content", None) or []:
            body += (getattr(c, "value", None) or "") + " "
        body += str(getattr(entry, "summary", None) or "")

    if search_in == "body":
        return match_fn(body)
    if search_in == "both":
        return match_fn(title) or match_fn(body)
    return match_fn(title)


def _run_now_dedup(
    conn: sqlite3.Connection,
    scope: str,
    scope_id: str,
    match_method: str,
    window_hours: int,
    max_per_feed: int = 500,
    exclude_scope_ids: str = "",
    fuzzy_threshold: float = 0.80,
    min_title_words: int = _DEDUP_MIN_TITLE_WORDS,
) -> dict:
    """Execute dedup rule on unread entries. Mark newer duplicates as read."""
    feed_urls = _resolve_dedup_feed_urls(conn, scope, scope_id, exclude_scope_ids)
    if isinstance(feed_urls, dict):
        return feed_urls  # {"error": ...}
    if len(feed_urls) < 2:
        return {"count": 0, "message": "Need at least 2 feeds in scope"}

    if match_method == "safe":
        false_rows = conn.execute("SELECT keep_link, mark_link FROM dedup_false_matches").fetchall()
        false_matches: set[str] = {r[0] + "||" + r[1] for r in false_rows}
        with get_reader() as reader:
            records = _safe_dedup_collect(reader, feed_urls, max_per_feed, False)
        pair_modes = _safe_dedup_find_pairs(records)
        link_to_rec = {r["link"]: r for r in records if r["link"]}
        to_mark: set[tuple[str, str]] = set()
        kept_keys: set[tuple[str, str]] = set()
        mark_to_keep: dict[tuple[str, str], str] = {}
        for (keep_link, mark_link), _modes in pair_modes.items():
            if keep_link + "||" + mark_link in false_matches:
                continue
            mark_rec = link_to_rec.get(mark_link)
            if mark_rec:
                to_mark.add((mark_rec["feed_url"], mark_rec["entry_id"]))
                mark_to_keep[(mark_rec["feed_url"], mark_rec["entry_id"])] = keep_link
                keep_rec = link_to_rec.get(keep_link)
                if keep_rec:
                    kept_keys.add((keep_rec["feed_url"], keep_rec["entry_id"]))
        with get_reader() as reader:
            for feed_url, entry_id in to_mark:
                reader.mark_entry_as_read((feed_url, entry_id))
        if to_mark:
            when = datetime.now().isoformat()
            conn.executemany(
                "INSERT INTO entry_read_state (feed_url, entry_id, read_at) VALUES (?, ?, ?)"
                " ON CONFLICT(feed_url, entry_id) DO UPDATE SET read_at = excluded.read_at",
                [(fu, eid, when) for fu, eid in to_mark],
            )
            _bump_unread_counts_generation()
        rec_map = {(r["feed_url"], r["entry_id"]): r for r in records}

        def _rec_info(fu: str, eid: str, matched_link: str | None = None) -> dict:
            return {
                "feed_url": fu,
                "entry_id": eid,
                "title": rec_map.get((fu, eid), {}).get("title", ""),
                "link": rec_map.get((fu, eid), {}).get("link", ""),
                "feed_title": rec_map.get((fu, eid), {}).get("feed_title", ""),
                "matched_link": matched_link,
            }

        matched_entries = [_rec_info(fu, eid, mark_to_keep.get((fu, eid))) for fu, eid in to_mark]
        kept_entries = [_rec_info(fu, eid, rec_map.get((fu, eid), {}).get("link", "")) for fu, eid in kept_keys - to_mark]
        return {"count": len(to_mark), "entries": matched_entries, "kept": kept_entries}

    slug_index: dict[str, list[dict]] = {}
    title_index: dict[str, list[dict]] = {}
    combined_index: dict[tuple[str, str], list[dict]] = {}
    fuzzy_entries: dict[str, list[dict]] = {}
    window_secs = window_hours * 3600
    _FUZZY_THRESHOLD = fuzzy_threshold

    with get_reader() as reader:
        for feed_url in feed_urls:
            try:
                for entry in reader.get_entries(feed=feed_url, read=False, limit=max_per_feed):
                    published = entry_effective_date(entry)
                    info = {
                        "feed_url": str(entry.feed_url or ""),
                        "entry_id": str(entry.id),
                        "link": str(entry.link or ""),
                        "title": str(entry.title or ""),
                        "feed_title": str(getattr(entry, "feed_resolved_title", None) or entry.feed_url or ""),
                        "published_ts": published.timestamp() if published else 0.0,
                    }
                    if match_method == "slug" and entry.link:
                        slug = entry_url_slug(entry.link)
                        if slug and len(slug) >= 4:
                            slug_index.setdefault(slug, []).append(info)
                    if match_method == "title" and entry.title:
                        norm = normalize_entry_title_for_dedupe(entry.title)
                        if norm and len(norm.split()) >= min_title_words:
                            title_index.setdefault(norm, []).append(info)
                    if match_method == "both" and entry.link and entry.title:
                        slug = entry_url_slug(entry.link)
                        norm = normalize_entry_title_for_dedupe(entry.title)
                        if slug and norm:
                            combined_index.setdefault((slug, norm), []).append(info)
                    if match_method == "fuzzy" and entry.title:
                        norm = normalize_entry_title_for_dedupe(entry.title)
                        if norm and len(norm.split()) >= min_title_words:
                            info["norm_title"] = norm
                            fuzzy_entries.setdefault(str(entry.feed_url or ""), []).append(info)
            except Exception:
                LOGGER.exception("run-now-dedup: error reading feed %s", feed_url)

        to_mark: set[tuple[str, str]] = set()
        kept_keys: set[tuple[str, str]] = set()
        # (marked key) -> the kept copy's link it matched, so run history can pair
        # each duplicate with its keeper. The keeper is always sorted_entries[0].
        mark_to_keep: dict[tuple[str, str], str] = {}

        if match_method == "slug":
            for _slug, entries in slug_index.items():
                if len({e["feed_url"] for e in entries}) < 2:
                    continue
                sorted_entries = sorted(entries, key=dedup_order_key)
                kept_keys.add((sorted_entries[0]["feed_url"], sorted_entries[0]["entry_id"]))
                for e in sorted_entries[1:]:
                    to_mark.add((e["feed_url"], e["entry_id"]))
                    mark_to_keep[(e["feed_url"], e["entry_id"])] = sorted_entries[0].get("link", "")

        if match_method == "title":
            for _norm_title, entries in title_index.items():
                if len({e["feed_url"] for e in entries}) < 2:
                    continue
                sorted_entries = sorted(entries, key=dedup_order_key)
                oldest_ts = sorted_entries[0]["published_ts"] or 0.0
                newest_ts = sorted_entries[-1]["published_ts"] or 0.0
                if oldest_ts > 0 and newest_ts > 0 and (newest_ts - oldest_ts) > window_secs:
                    continue
                kept_keys.add((sorted_entries[0]["feed_url"], sorted_entries[0]["entry_id"]))
                for e in sorted_entries[1:]:
                    to_mark.add((e["feed_url"], e["entry_id"]))
                    mark_to_keep[(e["feed_url"], e["entry_id"])] = sorted_entries[0].get("link", "")

        if match_method == "both":
            for (_slug, _norm_title), entries in combined_index.items():
                if len({e["feed_url"] for e in entries}) < 2:
                    continue
                sorted_entries = sorted(entries, key=dedup_order_key)
                oldest_ts = sorted_entries[0]["published_ts"] or 0.0
                newest_ts = sorted_entries[-1]["published_ts"] or 0.0
                if oldest_ts > 0 and newest_ts > 0 and (newest_ts - oldest_ts) > window_secs:
                    continue
                kept_keys.add((sorted_entries[0]["feed_url"], sorted_entries[0]["entry_id"]))
                for e in sorted_entries[1:]:
                    to_mark.add((e["feed_url"], e["entry_id"]))
                    mark_to_keep[(e["feed_url"], e["entry_id"])] = sorted_entries[0].get("link", "")

        if match_method == "fuzzy":
            feed_list = sorted(u for u in feed_urls if u in fuzzy_entries)
            for i, feed_i in enumerate(feed_list):
                for feed_j in feed_list[i + 1 :]:
                    for ei in fuzzy_entries[feed_i]:
                        for ej in fuzzy_entries[feed_j]:
                            ts_i = ei["published_ts"] or 0.0
                            ts_j = ej["published_ts"] or 0.0
                            if window_secs > 0 and abs(ts_i - ts_j) > window_secs:
                                continue
                            sim = title_word_similarity(ei["norm_title"], ej["norm_title"])
                            if sim < _FUZZY_THRESHOLD:
                                continue
                            newer = ej if dedup_order_key(ei) <= dedup_order_key(ej) else ei
                            older = ei if newer is ej else ej
                            to_mark.add((newer["feed_url"], newer["entry_id"]))
                            kept_keys.add((older["feed_url"], older["entry_id"]))
                            mark_to_keep[(newer["feed_url"], newer["entry_id"])] = older.get("link", "")

        for feed_url, entry_id in to_mark:
            reader.mark_entry_as_read((feed_url, entry_id))

    if to_mark:
        when = datetime.now().isoformat()
        conn.executemany(
            "INSERT INTO entry_read_state (feed_url, entry_id, read_at) VALUES (?, ?, ?)"
            " ON CONFLICT(feed_url, entry_id) DO UPDATE SET read_at = excluded.read_at",
            [(fu, eid, when) for fu, eid in to_mark],
        )
        _bump_unread_counts_generation()

    all_info = (
        list(slug_index.get(k, []) for k in slug_index)
        + list(title_index.get(k, []) for k in title_index)
        + list(combined_index.get(k, []) for k in combined_index)
        + list(fuzzy_entries.get(k, []) for k in fuzzy_entries)
    )
    entry_map = {(r["feed_url"], r["entry_id"]): r for sublist in all_info for r in sublist}

    def _entry_info(fu: str, eid: str, matched_link: str | None = None) -> dict:
        info = entry_map.get((fu, eid), {})
        return {
            "feed_url": fu,
            "entry_id": eid,
            "title": info.get("title", ""),
            "link": info.get("link", ""),
            "feed_title": info.get("feed_title", ""),
            # marked: the kept copy it matched; kept: its own link (group anchor).
            "matched_link": matched_link,
        }

    matched_entries = [_entry_info(fu, eid, mark_to_keep.get((fu, eid))) for fu, eid in to_mark]
    kept_entries = [_entry_info(fu, eid, entry_map.get((fu, eid), {}).get("link", "")) for fu, eid in kept_keys - to_mark]
    return {"count": len(to_mark), "entries": matched_entries, "kept": kept_entries}


def _run_now_pattern(
    conn: sqlite3.Connection,
    scope: str,
    scope_id: str,
    keyword: str,
    is_regex: bool,
    search_in: str,
) -> dict:
    """Execute mark_as_read rule: find matching unread entries and mark them read."""
    if not keyword:
        return {"count": 0}

    try:
        match_fn = build_keyword_matcher(keyword, is_regex)
    except re.error as e:
        return {"error": f"Invalid regex: {e}"}

    if scope == "folder":
        try:
            int(scope_id)
        except ValueError, TypeError:
            return {"error": "invalid scope_id"}
    elif scope == "folders" and not parse_folders_scope_id(scope_id):
        return {"error": "invalid scope_id"}
    feed_urls: set[str] | None = resolve_rule_feed_urls(conn, scope, scope_id)

    to_mark: list[tuple[str, str]] = []
    matched_entries: list[dict] = []
    _ENTRY_DETAIL_CAP = 50

    with get_reader() as reader:
        feed_title_cache: dict[str, str] = {}

        def iter_unread():
            if feed_urls is None:
                yield from reader.get_entries(read=False)
            else:
                for furl in feed_urls:
                    yield from reader.get_entries(feed=furl, read=False)

        for entry in iter_unread():
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
                fu = str(entry.feed_url or "")
                to_mark.append((fu, str(entry.id)))
                if len(matched_entries) < _ENTRY_DETAIL_CAP:
                    if fu not in feed_title_cache:
                        try:
                            f = reader.get_feed(fu)
                            feed_title_cache[fu] = feed_display_title(f, fu)
                        except Exception:
                            feed_title_cache[fu] = fu
                    matched_entries.append(
                        {
                            "feed_url": fu,
                            "entry_id": str(entry.id),
                            "title": str(entry.title or ""),
                            "link": str(entry.link or ""),
                            "feed_title": feed_title_cache.get(fu, fu),
                        }
                    )

        for feed_url, entry_id in to_mark:
            reader.mark_entry_as_read((feed_url, entry_id))

    if to_mark:
        when = datetime.now().isoformat()
        conn.executemany(
            "INSERT INTO entry_read_state (feed_url, entry_id, read_at) VALUES (?, ?, ?)"
            " ON CONFLICT(feed_url, entry_id) DO UPDATE SET read_at = excluded.read_at",
            [(fu, eid, when) for fu, eid in to_mark],
        )
        _bump_unread_counts_generation()

    return {"count": len(to_mark), "entries": matched_entries}


def _run_tag_filter(
    conn: sqlite3.Connection,
    scope: str,
    scope_id: str,
    spec: str,
    *,
    apply: bool = True,
) -> dict:
    """Execute (or dry-run) a tag_filter rule over entry_feed_tags with three
    tag strengths: ``-drop`` blocks; ``+good`` rescues from blocks without
    cutting anything by its absence ('+android, -iphone' keeps a post tagged
    with both, and Samsung posts still flow); ``++require`` cuts tagged
    entries lacking any required tag (opt-in whitelist). Untagged entries are
    always kept — a feed that stops tagging must not have its whole firehose
    suppressed.

    ``apply=False`` previews like _dry_run_pattern — read + unread entries,
    newest first — and returns the dry-run shape the Test panel renders
    (matches / total_scanned / total_matches / truncated)."""
    _DRY_MAX_ENTRIES = 1000
    _DRY_RESULT_LIMIT = 20

    require, good, exclude = parse_tag_filter_spec(spec)
    if not require and not good and not exclude:
        return {"error": "at least one tag is required (e.g. +python, -rust)"}
    saving = require | good  # either strength rescues from excludes

    if scope == "folder":
        try:
            int(scope_id)
        except ValueError, TypeError:
            return {"error": "invalid scope_id"}
    elif scope == "folders" and not parse_folders_scope_id(scope_id):
        return {"error": "invalid scope_id"}
    feed_urls: set[str] | None = resolve_rule_feed_urls(conn, scope, scope_id)

    to_mark: list[tuple[str, str]] = []
    matched_entries: list[dict] = []
    _ENTRY_DETAIL_CAP = 50
    # Entries a drop tag caught but a good/required tag let through, and which
    # tags did the rescuing — the dry run's explanation for an empty result.
    rescued = 0
    rescued_by: collections.Counter[str] = collections.Counter()

    with get_reader() as reader:
        feed_title_cache: dict[str, str] = {}
        # Normalized tag lookup per feed, loaded lazily one feed at a time.
        feed_tag_cache: dict[str, dict[str, set[str]]] = {}

        def entry_tags(feed_url: str, entry_id: str) -> set[str]:
            per_feed = feed_tag_cache.get(feed_url)
            if per_feed is None:
                per_feed = {}
                rows = conn.execute(
                    "SELECT entry_id, tag FROM entry_feed_tags WHERE feed_url = ?",
                    (feed_url,),
                ).fetchall()
                for row in rows:
                    normalized = normalize_tag_value(row["tag"])
                    if normalized:
                        per_feed.setdefault(str(row["entry_id"]), set()).add(normalized)
                feed_tag_cache[feed_url] = per_feed
            return per_feed.get(entry_id, set())

        def iter_entries():
            # Apply acts on unread only; dry-run previews read + unread so the
            # Test panel is useful even after the rule has already run.
            read_arg = False if apply else None
            if feed_urls is None:
                yield from reader.get_entries(read=read_arg)
            else:
                for furl in feed_urls:
                    yield from reader.get_entries(feed=furl, read=read_arg)

        def feed_title(fu: str) -> str:
            if fu not in feed_title_cache:
                try:
                    f = reader.get_feed(fu)
                    feed_title_cache[fu] = feed_display_title(f, fu)
                except Exception:
                    feed_title_cache[fu] = fu
            return feed_title_cache[fu]

        total_scanned = 0
        for entry in iter_entries():
            if not apply and total_scanned >= _DRY_MAX_ENTRIES:
                break
            total_scanned += 1
            fu = str(entry.feed_url or "")
            eid = str(entry.id)
            tags = entry_tags(fu, eid)
            # The author rides along as a pseudo-tag (by-<name>), so author
            # tokens work in every position: -by-x drops, +by-x rescues,
            # ++by-x requires. An authored-but-untagged entry is filterable.
            author_tok = author_filter_token(getattr(entry, "authors_str", None))
            if author_tok:
                tags = tags | {author_tok}
            if not tags:
                continue  # untagged (and authorless) entries are always kept
            if require and not (tags & require):
                pass  # whitelist mode: tagged entry lacks every required tag
            elif (tags & exclude) and not (tags & saving):
                pass  # blocked and nothing rescues it
            else:
                # Kept — but record WHY when a drop tag was overruled. A spec
                # like '-mac, +pc' reads as "drop Apple, keep PC" and on a feed
                # that tags platform availability every Mac post is also a PC
                # post, so the rescue silently cancels the whole rule. Zero
                # matches then looks identical to a rule that is working.
                if (tags & exclude) and (tags & saving):
                    rescued += 1
                    rescued_by.update(tags & saving)
                continue
            to_mark.append((fu, eid))
            if apply and len(matched_entries) < _ENTRY_DETAIL_CAP:
                matched_entries.append(
                    {
                        "feed_url": fu,
                        "entry_id": eid,
                        "title": str(entry.title or ""),
                        "link": str(entry.link or ""),
                        "feed_title": feed_title(fu),
                    }
                )
            elif not apply and len(matched_entries) < _DRY_RESULT_LIMIT:
                published = entry_effective_date(entry)
                matched_entries.append(
                    {
                        "feed_url": fu,
                        "entry_id": eid,
                        "title": str(entry.title or ""),
                        "link": str(entry.link or ""),
                        "feed_title": feed_title(fu),
                        "published": published.isoformat() if published else None,
                        "read": bool(entry.read),
                    }
                )

        if apply:
            for feed_url, entry_id in to_mark:
                reader.mark_entry_as_read((feed_url, entry_id))

    if apply and to_mark:
        when = datetime.now().isoformat()
        conn.executemany(
            "INSERT INTO entry_read_state (feed_url, entry_id, read_at) VALUES (?, ?, ?)"
            " ON CONFLICT(feed_url, entry_id) DO UPDATE SET read_at = excluded.read_at",
            [(fu, eid, when) for fu, eid in to_mark],
        )
        _bump_unread_counts_generation()

    if not apply:
        return {
            "matches": matched_entries,
            "total_scanned": total_scanned,
            "total_matches": len(to_mark),
            "truncated": len(to_mark) > _DRY_RESULT_LIMIT,
            "count": len(to_mark),
            "rescued": rescued,
            "rescued_by": [t for t, _n in rescued_by.most_common(4)],
            # A spec of nothing but good tags cuts nothing, ever, by design:
            # good tags rescue from drops and whitelist nothing, so with no
            # drops to rescue from there is nothing for them to do. It is a
            # reasonable thing to write ("+wallpapers" reads as "keep these"),
            # and its result is indistinguishable from a rule that is working,
            # so the preview names it rather than reporting a bare zero.
            "good_only": bool(good and not exclude and not require),
        }
    return {"count": len(to_mark), "entries": matched_entries}


_EMAIL_AUTO_PER_RUN_CAP = 10  # max immediate emails per refresh cycle


def _run_email_rules_after_refresh(refreshed_feed_urls: set[str]) -> None:
    """Send or queue email_article rule matches for freshly-refreshed feeds."""
    if not is_email_configured():
        return
    if not refreshed_feed_urls:
        return

    try:
        from datetime import timedelta
        from datetime import timezone as _tz

        cutoff = datetime.now(_tz.utc) - timedelta(minutes=15)

        with get_meta_connection() as conn:
            all_rules = get_highlight_keywords(conn)
            profile_email = get_setting(conn, PROFILE_EMAIL_SETTING_KEY) or ""
            folder_ids_needed: set[int] = set()
            for r in all_rules:
                if r.get("enabled"):
                    folder_ids_needed |= rule_scope_folder_ids(str(r.get("scope", "")), str(r.get("scope_id") or ""))
            folder_feed_map: dict[int, set[str]] = {fid: get_folder_feed_urls(conn, fid) for fid in folder_ids_needed}

        email_rules = [r for r in all_rules if r.get("enabled") and r.get("type") == "email_article" and r.get("email_to")]
        if not email_rules:
            return

        immediate_sent = 0
        now_str = datetime.now().isoformat()

        for rule in email_rules:
            try:
                scope = str(rule.get("scope", ""))
                scope_id = str(rule.get("scope_id") or "")
                keyword = str(rule.get("keyword", ""))
                rule_uid = str(rule.get("rule_uid") or "")
                is_regex = bool(rule.get("is_regex"))
                search_in = str(rule.get("search_in") or "title")
                delivery = str(rule.get("delivery") or "immediately")
                email_to = str(rule.get("email_to") or "")
                batch_count = int(rule.get("batch_count") or 0)
                cc_me = bool(rule.get("cc_me"))
                # Suppress Cc when profile email is already the To recipient
                cc_addr = profile_email if cc_me and profile_email and profile_email.lower() != email_to.lower() else None

                with get_reader() as reader:
                    feed_title_cache: dict[str, str] = {}

                    for feed_url in refreshed_feed_urls:
                        # Scope check
                        _folder_set = rule_scope_folder_feed_set(scope, scope_id, folder_feed_map)
                        in_scope = feed_in_rule_scope(scope, scope_id, feed_url, _folder_set)
                        if not in_scope:
                            continue

                        for entry in reader.get_entries(feed=feed_url):
                            added = getattr(entry, "added", None)
                            if not added or added < cutoff:
                                continue
                            if not _entry_matches_rule(entry, keyword, is_regex, search_in):
                                continue

                            fu = str(entry.feed_url or "")
                            if fu not in feed_title_cache:
                                try:
                                    f = reader.get_feed(fu)
                                    feed_title_cache[fu] = feed_display_title(f, fu)
                                except Exception:
                                    feed_title_cache[fu] = fu

                            article = {
                                "feed_url": fu,
                                "entry_id": str(entry.id),
                                "title": str(entry.title or ""),
                                "link": str(entry.link or ""),
                                "feed_title": feed_title_cache.get(fu, fu),
                                "excerpt": _get_entry_excerpt(entry),
                            }

                            if delivery == "immediately":
                                if immediate_sent >= _EMAIL_AUTO_PER_RUN_CAP:
                                    continue
                                ok, err = send_article_email(
                                    get_resend_api_key(),
                                    get_resend_from(),
                                    email_to,
                                    article["title"],
                                    article["feed_title"],
                                    article["link"],
                                    article["excerpt"],
                                    cc_addr=cc_addr,
                                )
                                if ok:
                                    immediate_sent += 1
                                    with get_meta_connection() as conn:
                                        _log_auto_run(
                                            conn,
                                            now_str,
                                            "email_article",
                                            scope,
                                            scope_id,
                                            keyword,
                                            {
                                                "count": 1,
                                                "entries": [article],
                                            },
                                        )
                                else:
                                    LOGGER.warning("[email-auto] send failed: %s", err)
                            else:
                                # batch mode — queue for digest
                                with get_meta_connection() as conn:
                                    conn.execute(
                                        "INSERT OR IGNORE INTO email_batch_queue"
                                        " (rule_scope, rule_scope_id, rule_keyword, rule_uid, queued_at,"
                                        "  feed_url, entry_id, title, link, feed_title, excerpt,"
                                        "  email_to, cc_me)"
                                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                        (
                                            scope,
                                            scope_id,
                                            keyword,
                                            rule_uid,
                                            now_str,
                                            fu,
                                            article["entry_id"],
                                            article["title"],
                                            article["link"],
                                            article["feed_title"],
                                            article["excerpt"],
                                            email_to,
                                            1 if cc_me else 0,
                                        ),
                                    )
                                    # Flush immediately if batch_count threshold is reached.
                                    # Counted by rule_uid when this rule has one (always, in
                                    # practice) so a scope/keyword edit mid-backlog still counts
                                    # entries queued under the old text as part of the same rule,
                                    # rather than splitting the count across two identities.
                                    if batch_count > 0:
                                        if rule_uid:
                                            pending = conn.execute(
                                                "SELECT COUNT(*) FROM email_batch_queue WHERE rule_uid=? AND email_to=?",
                                                (rule_uid, email_to),
                                            ).fetchone()[0]
                                        else:
                                            pending = conn.execute(
                                                "SELECT COUNT(*) FROM email_batch_queue"
                                                " WHERE rule_scope=? AND rule_scope_id=? AND rule_keyword=?"
                                                " AND email_to=?",
                                                (scope, scope_id, keyword, email_to),
                                            ).fetchone()[0]
                                        if pending >= batch_count:
                                            _flush_email_batch_for_rule(
                                                conn,
                                                scope,
                                                scope_id,
                                                keyword,
                                                email_to,
                                                cc_addr,
                                                now_str,
                                                rule_uid=rule_uid,
                                            )
            except Exception:
                LOGGER.exception("[email-auto] error processing email rule %s/%s", scope, keyword)
    except Exception:
        LOGGER.exception("[email-auto] error in _run_email_rules_after_refresh")


_WEBHOOK_AUTO_PER_RUN_CAP = 50  # max webhook POSTs per refresh cycle


def _run_webhook_rules_after_refresh(refreshed_feed_urls: set[str]) -> None:
    """POST webhook-rule matches for freshly-refreshed feeds (immediate delivery)."""
    if not refreshed_feed_urls:
        return

    try:
        from datetime import timedelta
        from datetime import timezone as _tz

        cutoff = datetime.now(_tz.utc) - timedelta(minutes=15)

        with get_meta_connection() as conn:
            all_rules = get_highlight_keywords(conn)
            folder_ids_needed: set[int] = set()
            for r in all_rules:
                if r.get("enabled"):
                    folder_ids_needed |= rule_scope_folder_ids(str(r.get("scope", "")), str(r.get("scope_id") or ""))
            folder_feed_map: dict[int, set[str]] = {fid: get_folder_feed_urls(conn, fid) for fid in folder_ids_needed}

        webhook_rules = [r for r in all_rules if r.get("enabled") and r.get("type") == "webhook" and r.get("webhook_url")]
        if not webhook_rules:
            return

        sent = 0
        now_str = datetime.now().isoformat()

        for rule in webhook_rules:
            try:
                scope = str(rule.get("scope", ""))
                scope_id = str(rule.get("scope_id") or "")
                keyword = str(rule.get("keyword", ""))
                is_regex = bool(rule.get("is_regex"))
                search_in = str(rule.get("search_in") or "title")
                webhook_url = str(rule.get("webhook_url") or "")
                webhook_format = str(rule.get("webhook_format") or "generic")
                webhook_batch = bool(rule.get("webhook_batch"))

                with get_reader() as reader:
                    feed_title_cache: dict[str, str] = {}
                    batch_articles: list[dict] = []

                    for feed_url in refreshed_feed_urls:
                        _folder_set = rule_scope_folder_feed_set(scope, scope_id, folder_feed_map)
                        in_scope = feed_in_rule_scope(scope, scope_id, feed_url, _folder_set)
                        if not in_scope:
                            continue

                        for entry in reader.get_entries(feed=feed_url):
                            added = getattr(entry, "added", None)
                            if not added or added < cutoff:
                                continue
                            if not _entry_matches_rule(entry, keyword, is_regex, search_in):
                                continue
                            if sent >= _WEBHOOK_AUTO_PER_RUN_CAP:
                                continue

                            fu = str(entry.feed_url or "")
                            if fu not in feed_title_cache:
                                try:
                                    f = reader.get_feed(fu)
                                    feed_title_cache[fu] = feed_display_title(f, fu)
                                except Exception:
                                    feed_title_cache[fu] = fu

                            published = getattr(entry, "published", None) or getattr(entry, "updated", None)
                            article = {
                                "feed_url": fu,
                                "entry_id": str(entry.id),
                                "title": str(entry.title or ""),
                                "link": str(entry.link or ""),
                                "feed_title": feed_title_cache.get(fu, fu),
                                "excerpt": _get_entry_excerpt(entry),
                                "published": published.isoformat() if published else "",
                                "tags": get_manual_tags_for_entry(fu, str(entry.id)),
                            }

                            if webhook_batch:
                                batch_articles.append(article)
                                sent += 1
                            else:
                                payload = build_webhook_payload(article, webhook_format)
                                ok, err = send_webhook(webhook_url, payload)
                                if ok:
                                    sent += 1
                                    with get_meta_connection() as conn:
                                        _log_auto_run(
                                            conn,
                                            now_str,
                                            "webhook",
                                            scope,
                                            scope_id,
                                            keyword,
                                            {
                                                "count": 1,
                                                "entries": [article],
                                            },
                                        )
                                else:
                                    LOGGER.warning("[webhook-auto] POST failed: %s", err)

                if webhook_batch and batch_articles:
                    payload = build_webhook_batch_payload(batch_articles, webhook_format)
                    ok, err = send_webhook(webhook_url, payload)
                    if ok:
                        with get_meta_connection() as conn:
                            _log_auto_run(
                                conn,
                                now_str,
                                "webhook",
                                scope,
                                scope_id,
                                keyword,
                                {
                                    "count": len(batch_articles),
                                    "entries": batch_articles,
                                },
                            )
                    else:
                        LOGGER.warning("[webhook-auto] batch POST failed: %s", err)
            except Exception:
                LOGGER.exception("[webhook-auto] error processing webhook rule %s/%s", scope, keyword)
    except Exception:
        LOGGER.exception("[webhook-auto] error in _run_webhook_rules_after_refresh")


_INSTAPAPER_AUTO_PER_RUN_CAP = 50  # max Instapaper saves per refresh cycle


def _run_instapaper_rules_after_refresh(refreshed_feed_urls: set[str]) -> None:
    """Save matching freshly-refreshed entries to Instapaper. Instapaper dedupes by
    URL, so re-saves are harmless; the 15-min cutoff + per-run cap bound the calls."""
    if not refreshed_feed_urls:
        return
    username = get_runtime_setting(SETTING_INSTAPAPER_USERNAME).strip()
    password = get_runtime_setting(SETTING_INSTAPAPER_PASSWORD).strip()
    if not (username and password):
        return

    try:
        from datetime import timedelta
        from datetime import timezone as _tz

        cutoff = datetime.now(_tz.utc) - timedelta(minutes=15)

        with get_meta_connection() as conn:
            all_rules = get_highlight_keywords(conn)
            folder_ids_needed: set[int] = set()
            for r in all_rules:
                if r.get("enabled"):
                    folder_ids_needed |= rule_scope_folder_ids(str(r.get("scope", "")), str(r.get("scope_id") or ""))
            folder_feed_map: dict[int, set[str]] = {fid: get_folder_feed_urls(conn, fid) for fid in folder_ids_needed}

        rules = [r for r in all_rules if r.get("enabled") and r.get("type") == "instapaper"]
        if not rules:
            return

        sent = 0
        now_str = datetime.now().isoformat()
        for rule in rules:
            try:
                scope = str(rule.get("scope", ""))
                scope_id = str(rule.get("scope_id") or "")
                keyword = str(rule.get("keyword", ""))
                is_regex = bool(rule.get("is_regex"))
                search_in = str(rule.get("search_in") or "title")
                with get_reader() as reader:
                    feed_title_cache: dict[str, str] = {}
                    for feed_url in refreshed_feed_urls:
                        _folder_set = rule_scope_folder_feed_set(scope, scope_id, folder_feed_map)
                        if not feed_in_rule_scope(scope, scope_id, feed_url, _folder_set):
                            continue
                        for entry in reader.get_entries(feed=feed_url):
                            if sent >= _INSTAPAPER_AUTO_PER_RUN_CAP:
                                break
                            added = getattr(entry, "added", None)
                            if not added or added < cutoff:
                                continue
                            # Empty keyword = save every new entry in scope.
                            if keyword and not _entry_matches_rule(entry, keyword, is_regex, search_in):
                                continue
                            link = str(entry.link or "")
                            if not link:
                                continue
                            ok, err = _instapaper_save_url(username, password, link, str(entry.title or ""))
                            if not ok:
                                LOGGER.warning("[instapaper-auto] save failed: %s", err)
                                continue
                            sent += 1
                            fu = str(entry.feed_url or "")
                            if fu not in feed_title_cache:
                                try:
                                    feed_title_cache[fu] = str(getattr(reader.get_feed(fu), "title", None) or fu)
                                except Exception:
                                    feed_title_cache[fu] = fu
                            with get_meta_connection() as conn:
                                _log_auto_run(
                                    conn,
                                    now_str,
                                    "instapaper",
                                    scope,
                                    scope_id,
                                    keyword,
                                    {
                                        "count": 1,
                                        "entries": [
                                            {
                                                "feed_url": fu,
                                                "entry_id": str(entry.id),
                                                "title": str(entry.title or ""),
                                                "link": link,
                                                "feed_title": feed_title_cache.get(fu, fu),
                                            }
                                        ],
                                    },
                                )
            except Exception:
                LOGGER.exception("[instapaper-auto] error processing rule %s/%s", scope, keyword)
    except Exception:
        LOGGER.exception("[instapaper-auto] error in _run_instapaper_rules_after_refresh")


_SAVE_ARTICLE_AUTO_PER_RUN_CAP = 50  # archive worker fan-out bound
# NB: saving no longer writes an "inbox" tag. The Saved Inbox is defined by the
# STAR (kept=starred) since 2026-08-03, so the tag stopped carrying meaning and
# only fought the user: untagging a post put it back on the next save, because
# a re-save counts as new or "resurfaced". Existing `inbox` tags are left alone
# — they are the user's data, and the Tags list still lists them.


def _run_save_article_rules_after_refresh(refreshed_feed_urls: set[str]) -> None:
    """Star matching freshly-refreshed entries into the Saved backlog and tag
    them 'inbox' (the Lectio-native counterpart of the Instapaper rule).

    Idempotent per entry: an already-starred entry is skipped, so re-refreshes
    never re-tag something the user already filed out of the Inbox. Bounded by
    the 15-min added cutoff and a per-run cap like the other save-out rules."""
    if not refreshed_feed_urls:
        return
    try:
        from datetime import timedelta
        from datetime import timezone as _tz

        cutoff = datetime.now(_tz.utc) - timedelta(minutes=15)

        with get_meta_connection() as conn:
            all_rules = get_highlight_keywords(conn)
            folder_ids_needed: set[int] = set()
            for r in all_rules:
                if r.get("enabled"):
                    folder_ids_needed |= rule_scope_folder_ids(str(r.get("scope", "")), str(r.get("scope_id") or ""))
            folder_feed_map: dict[int, set[str]] = {fid: get_folder_feed_urls(conn, fid) for fid in folder_ids_needed}

        rules = [r for r in all_rules if r.get("enabled") and r.get("type") == "save_article"]
        if not rules:
            return

        saved = 0
        now_str = datetime.now().isoformat()
        for rule in rules:
            try:
                scope = str(rule.get("scope", ""))
                scope_id = str(rule.get("scope_id") or "")
                keyword = str(rule.get("keyword", ""))
                is_regex = bool(rule.get("is_regex"))
                search_in = str(rule.get("search_in") or "title")
                with get_reader() as reader:
                    feed_title_cache: dict[str, str] = {}
                    for feed_url in refreshed_feed_urls:
                        _folder_set = rule_scope_folder_feed_set(scope, scope_id, folder_feed_map)
                        if not feed_in_rule_scope(scope, scope_id, feed_url, _folder_set):
                            continue
                        for entry in reader.get_entries(feed=feed_url):
                            if saved >= _SAVE_ARTICLE_AUTO_PER_RUN_CAP:
                                break
                            added = getattr(entry, "added", None)
                            if not added or added < cutoff:
                                continue
                            # Empty keyword = save every new entry in scope.
                            if keyword and not _entry_matches_rule(entry, keyword, is_regex, search_in):
                                continue
                            fu = str(entry.feed_url or "")
                            eid = str(entry.id)
                            result = _star_entry_for_current_user(fu, eid)
                            if not result.get("ok") or result.get("duplicate"):
                                continue  # missing, or already in Saved
                            saved += 1
                            if fu not in feed_title_cache:
                                try:
                                    feed_title_cache[fu] = str(getattr(reader.get_feed(fu), "title", None) or fu)
                                except Exception:
                                    feed_title_cache[fu] = fu
                            with get_meta_connection() as conn:
                                _log_auto_run(
                                    conn,
                                    now_str,
                                    "save_article",
                                    scope,
                                    scope_id,
                                    keyword,
                                    {
                                        "count": 1,
                                        "entries": [
                                            {
                                                "feed_url": fu,
                                                "entry_id": eid,
                                                "title": str(entry.title or ""),
                                                "link": str(entry.link or ""),
                                                "feed_title": feed_title_cache.get(fu, fu),
                                            }
                                        ],
                                    },
                                )
            except Exception:
                LOGGER.exception("[save-article-auto] error processing rule %s/%s", scope, keyword)
    except Exception:
        LOGGER.exception("[save-article-auto] error in _run_save_article_rules_after_refresh")


# Cap a run well under the Free-tier 50/min so an automation burst never trips the
# Quire rate limit; the sliding-window meter is also consulted before each add.
_QUIRE_AUTO_PER_RUN_CAP = 20


def _run_quire_rules_after_refresh(refreshed_feed_urls: set[str]) -> None:
    """Add matching freshly-refreshed entries as tasks to the default Quire project.
    Bounded by the 15-min cutoff, a per-run cap, the usage meter, and 429 backoff."""
    if not refreshed_feed_urls:
        return
    if not is_quire_configured():
        return
    project_oid = quire_project_oid()
    token = get_quire_user_token()
    if not token:
        return

    try:
        from datetime import timedelta
        from datetime import timezone as _tz

        cutoff = datetime.now(_tz.utc) - timedelta(minutes=15)

        with get_meta_connection() as conn:
            all_rules = get_highlight_keywords(conn)
            folder_ids_needed: set[int] = set()
            for r in all_rules:
                if r.get("enabled"):
                    folder_ids_needed |= rule_scope_folder_ids(str(r.get("scope", "")), str(r.get("scope_id") or ""))
            folder_feed_map: dict[int, set[str]] = {fid: get_folder_feed_urls(conn, fid) for fid in folder_ids_needed}

        rules = [r for r in all_rules if r.get("enabled") and r.get("type") == "quire"]
        if not rules:
            return

        sent = 0
        now_str = datetime.now().isoformat()
        for rule in rules:
            if sent >= _QUIRE_AUTO_PER_RUN_CAP:
                break
            try:
                scope = str(rule.get("scope", ""))
                scope_id = str(rule.get("scope_id") or "")
                keyword = str(rule.get("keyword", ""))
                is_regex = bool(rule.get("is_regex"))
                search_in = str(rule.get("search_in") or "title")
                with get_reader() as reader:
                    feed_title_cache: dict[str, str] = {}
                    for feed_url in refreshed_feed_urls:
                        _folder_set = rule_scope_folder_feed_set(scope, scope_id, folder_feed_map)
                        if not feed_in_rule_scope(scope, scope_id, feed_url, _folder_set):
                            continue
                        for entry in reader.get_entries(feed=feed_url):
                            if sent >= _QUIRE_AUTO_PER_RUN_CAP:
                                break
                            if get_quire_usage_status()["state"] == "blocked":
                                LOGGER.warning("[quire-auto] rate limit reached; %d added this run", sent)
                                return
                            added = getattr(entry, "added", None)
                            if not added or added < cutoff:
                                continue
                            if keyword and not _entry_matches_rule(entry, keyword, is_regex, search_in):
                                continue
                            link = str(entry.link or "")
                            if not link:
                                continue
                            fu = str(entry.feed_url or "")
                            if fu not in feed_title_cache:
                                try:
                                    feed_title_cache[fu] = str(getattr(reader.get_feed(fu), "title", None) or fu)
                                except Exception:
                                    feed_title_cache[fu] = fu
                            ok, err = _quire_add_entry(token, project_oid, str(entry.title or ""), link, feed_title_cache.get(fu, fu))
                            if not ok:
                                LOGGER.warning("[quire-auto] add failed: %s", err)
                                if isinstance(err, str) and "rate limit" in err.lower():
                                    return  # back off the whole run on 429
                                continue
                            sent += 1
                            with get_meta_connection() as conn:
                                _log_auto_run(
                                    conn,
                                    now_str,
                                    "quire",
                                    scope,
                                    scope_id,
                                    keyword,
                                    {
                                        "count": 1,
                                        "entries": [
                                            {
                                                "feed_url": fu,
                                                "entry_id": str(entry.id),
                                                "title": str(entry.title or ""),
                                                "link": link,
                                                "feed_title": feed_title_cache.get(fu, fu),
                                            }
                                        ],
                                    },
                                )
            except Exception:
                LOGGER.exception("[quire-auto] error processing rule %s/%s", scope, keyword)
    except Exception:
        LOGGER.exception("[quire-auto] error in _run_quire_rules_after_refresh")


# Each playlistItems.insert costs 50 quota units; cap a run well under the daily
# 10k so auto-add never exhausts the quota on a burst of new uploads.
_YT_PLAYLIST_AUTO_PER_RUN_CAP = 25


def _apply_youtube_playlist_rules(
    refreshed_feed_urls: set[str],
    cutoff: datetime,
    yt_rules: list[dict],
    folder_feed_map: dict[int, set[str]],
    token: str,
    *,
    trigger: str = "auto",
) -> int:
    """Match+add entries' YouTube videos to each rule's target playlist.

    Shared by the after-refresh automation (``cutoff`` = the persisted
    watermark, ``refreshed_feed_urls`` = whatever this tick refreshed) and the
    one-off backfill script (``cutoff`` = an arbitrary historical date,
    ``refreshed_feed_urls`` = a rule's whole scope) — the matching, dedup-guard,
    and quota handling are identical either way. Returns videos added.
    """
    added_total = 0
    now_str = datetime.now().isoformat()

    for rule in yt_rules:
        if added_total >= _YT_PLAYLIST_AUTO_PER_RUN_CAP:
            break
        scope = str(rule.get("scope", ""))
        scope_id = str(rule.get("scope_id") or "")
        keyword = str(rule.get("keyword", ""))
        rule_uid = str(rule.get("rule_uid") or "")
        is_regex = bool(rule.get("is_regex"))
        search_in = str(rule.get("search_in") or "title")
        playlist_id = str(rule.get("yt_playlist_id") or "")
        include_shorts = bool(rule.get("yt_include_shorts"))
        mark_read = bool(rule.get("yt_mark_read"))
        min_secs = max(0, int(rule.get("yt_min_minutes") or 0)) * 60
        max_secs = max(0, int(rule.get("yt_max_minutes") or 0)) * 60
        run_entries: list[dict] = []
        marked: list[tuple[str, str]] = []
        quota_hit = False
        try:
            with get_reader() as reader:
                feed_title_cache: dict[str, str] = {}
                for feed_url in refreshed_feed_urls:
                    _folder_set = rule_scope_folder_feed_set(scope, scope_id, folder_feed_map)
                    in_scope = feed_in_rule_scope(scope, scope_id, feed_url, _folder_set)
                    if not in_scope:
                        continue

                    for entry in reader.get_entries(feed=feed_url):
                        if added_total >= _YT_PLAYLIST_AUTO_PER_RUN_CAP:
                            break
                        added = getattr(entry, "added", None)
                        if not added or added < cutoff:
                            continue
                        # Empty keyword = add every new video in scope.
                        if keyword and not _entry_matches_rule(entry, keyword, is_regex, search_in):
                            continue
                        if not include_shorts and _is_youtube_short(entry):
                            continue
                        link = str(entry.link or "")
                        body = "".join((c.value or "") for c in (entry.content or []))
                        body += str(entry.summary or "")
                        vids = youtube_embeds.video_ids_in_text(link, body)
                        if not vids:
                            continue
                        fu = str(entry.feed_url or "")
                        eid = str(entry.id)
                        entry_added_any = False
                        for vid in vids:
                            if added_total >= _YT_PLAYLIST_AUTO_PER_RUN_CAP:
                                break
                            # Duration filter (minutes; 0 = no limit). The video's
                            # length comes from the same cache that powers the
                            # [duration] title prefix; an unknown duration is skipped
                            # this run (it's retried once the duration is cached).
                            if min_secs or max_secs:
                                dur = youtube_duration_service.get_cached_duration(vid)[0]
                                if dur is None:
                                    continue
                                if min_secs and dur < min_secs:
                                    continue
                                if max_secs and dur > max_secs:
                                    continue
                            # Dedup guard: claim the (rule, entry, video) row first;
                            # rowcount 0 means we've added it before — skip. Written
                            # against BOTH identities (the legacy scope/scope_id/
                            # keyword PK and the rule_uid partial unique index, when
                            # rule_uid is set) — OR IGNORE backs off on a conflict
                            # with either, so a rule edited since its last add still
                            # dedupes correctly instead of re-submitting.
                            with get_meta_connection() as conn:
                                cur = conn.execute(
                                    "INSERT OR IGNORE INTO youtube_playlist_added"
                                    " (scope, scope_id, keyword, entry_id, video_id, added_at, rule_uid)"
                                    " VALUES (?,?,?,?,?,?,?)",
                                    (scope, scope_id, keyword, eid, vid, now_str, rule_uid),
                                )
                                claimed = cur.rowcount > 0
                            if not claimed:
                                continue
                            try:
                                youtube_oauth_service.add_video_to_playlist(token, playlist_id, vid)
                                added_total += 1
                                entry_added_any = True
                            except youtube_oauth_service.QuotaExceeded:
                                # Release the claim so it retries once quota resets,
                                # and stop the whole run.
                                with get_meta_connection() as conn:
                                    conn.execute(
                                        "DELETE FROM youtube_playlist_added"
                                        " WHERE scope=? AND scope_id=? AND keyword=? AND entry_id=? AND video_id=?",
                                        (scope, scope_id, keyword, eid, vid),
                                    )
                                mark_yt_quota_exhausted()
                                LOGGER.warning("[yt-playlist-auto] quota exceeded; %d added this run", added_total)
                                raise
                            except Exception as exc:  # noqa: BLE001
                                with get_meta_connection() as conn:
                                    conn.execute(
                                        "DELETE FROM youtube_playlist_added"
                                        " WHERE scope=? AND scope_id=? AND keyword=? AND entry_id=? AND video_id=?",
                                        (scope, scope_id, keyword, eid, vid),
                                    )
                                LOGGER.warning("[yt-playlist-auto] add failed for %s: %s", vid, exc)
                        if entry_added_any:
                            if fu not in feed_title_cache:
                                try:
                                    feed_title_cache[fu] = str(getattr(reader.get_feed(fu), "title", None) or fu)
                                except Exception:
                                    feed_title_cache[fu] = fu
                            run_entries.append(
                                {
                                    "feed_url": fu,
                                    "entry_id": eid,
                                    "title": str(entry.title or ""),
                                    "link": link,
                                    "feed_title": feed_title_cache.get(fu, fu),
                                }
                            )
                            if mark_read:
                                reader.mark_entry_as_read((fu, eid))
                                marked.append((fu, eid))
        except youtube_oauth_service.QuotaExceeded:
            quota_hit = True
        except Exception:
            LOGGER.exception("[yt-playlist-auto] error processing rule %s/%s", scope, keyword)

        if marked:
            when = datetime.now().isoformat()
            with get_meta_connection() as conn:
                conn.executemany(
                    "INSERT INTO entry_read_state (feed_url, entry_id, read_at) VALUES (?, ?, ?)"
                    " ON CONFLICT(feed_url, entry_id) DO UPDATE SET read_at = excluded.read_at",
                    [(fu, eid, when) for fu, eid in marked],
                )
            _bump_unread_counts_generation()
        if run_entries:
            with get_meta_connection() as conn:
                _log_auto_run(
                    conn,
                    now_str,
                    "youtube_playlist",
                    scope,
                    scope_id,
                    keyword,
                    {
                        "count": len(run_entries),
                        "entries": run_entries,
                    },
                    trigger=trigger,
                )
        if quota_hit:
            # Quota is exhausted for the day (units are cumulative across
            # rules) — trying the next rule would just fail the same way.
            LOGGER.warning("[yt-playlist-auto] quota exceeded; stopping remaining rules this run (%d added)", added_total)
            break

    return added_total


def _run_youtube_playlist_rules_after_refresh(refreshed_feed_urls: set[str]) -> None:
    """Add newly-refreshed matching entries' YouTube videos to a target playlist.

    A YouTube video can be embedded in any feed, so this is a general rule (any
    feed/folder), and one entry can carry several videos. Extracts all video ids
    from the entry link + content, inserts each into the rule's playlist, and
    optionally marks the post read. Non-idempotent inserts are guarded by the
    youtube_playlist_added table so a video is never added twice.
    """
    if not refreshed_feed_urls:
        return

    try:
        from datetime import timedelta
        from datetime import timezone as _tz

        # Cutoff is a persisted watermark — "everything added since the last
        # time this ran" — not a fixed "now minus N minutes" window. A fixed
        # window silently and PERMANENTLY drops any entry ingested earlier
        # than the window relative to whenever this function happens to run:
        # it's called once after a whole scheduled refresh batch completes,
        # and a large batch (hundreds of feeds, paced) can easily take longer
        # than a short fixed window — the entry's `added` timestamp never
        # moves, so a missed entry is missed forever, not retried next run.
        # Confirmed live 2026-08-28: only ~1-in-7 of one channel's qualifying
        # videos over a week had actually been added, the rest silently lost
        # to exactly this gap. Captured BEFORE the loop (not after) so the
        # next run's watermark can't itself open a gap while this run works.
        run_started_at = datetime.now(_tz.utc)
        with get_meta_connection() as conn:
            last_check_raw = get_setting(conn, SETTING_YT_PLAYLIST_AUTO_LAST_CHECK)
        cutoff = None
        if last_check_raw:
            try:
                cutoff = datetime.fromisoformat(last_check_raw)
                if cutoff.tzinfo is None:
                    cutoff = cutoff.replace(tzinfo=_tz.utc)
            except ValueError, TypeError:
                cutoff = None
        if cutoff is None:
            # First run ever (or a corrupt watermark) — the original fixed
            # window, so this doesn't suddenly bulk-add a backlog of every
            # matching video ever ingested.
            cutoff = run_started_at - timedelta(minutes=15)

        with get_meta_connection() as conn:
            all_rules = get_highlight_keywords(conn)
            yt_rules = [r for r in all_rules if r.get("enabled") and r.get("type") == "youtube_playlist" and r.get("yt_playlist_id")]
            if not yt_rules:
                return
            folder_feed_map: dict[int, set[str]] = {}
            for r in yt_rules:
                for fid in rule_scope_folder_ids(str(r.get("scope", "")), str(r.get("scope_id") or "")):
                    if fid not in folder_feed_map:
                        folder_feed_map[fid] = get_folder_feed_urls(conn, fid)

        token = get_youtube_oauth_token()
        if not token:
            LOGGER.warning("[yt-playlist-auto] %d rule(s) enabled but no YouTube token — reconnect needed", len(yt_rules))
            return

        _apply_youtube_playlist_rules(refreshed_feed_urls, cutoff, yt_rules, folder_feed_map, token)

        # Advance the watermark to when THIS run started, not to "now" —
        # captured up front so a slow run can't itself open a gap. A per-rule
        # quota exhaustion doesn't affect this: entries that failed to add
        # already had their dedup claim rolled back above, so they retry via
        # the dedup guard regardless of where the time watermark sits next.
        with get_meta_connection() as conn:
            set_setting(conn, SETTING_YT_PLAYLIST_AUTO_LAST_CHECK, run_started_at.isoformat())
    except Exception:
        LOGGER.exception("[yt-playlist-auto] error in _run_youtube_playlist_rules_after_refresh")
