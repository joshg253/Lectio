"""Post-refresh automation rule execution: run-log writer, entry excerpting,
keyword-rule matching, and the three user-triggerable "run now" rule executors
(deduplicate/mark_as_read/tag_filter) -- shared by every automation dispatcher
(mark_as_read, dedup, email/webhook/instapaper/quire/save_article/youtube_playlist)
and by the manual Run Now / dry-run routes still resident in main.py.

Extracted out of main.py (Plan.md's main.py/index.html breakup, Step 2, Stages B-C).
This module does `from main import ...` for a handful of main.py-resident
primitives (dedup/pattern helpers staying in main.py -- see Plan.md's Step 4,
`services/dedup.py` -- plus scope-resolution and tag helpers with no other home
yet) -- it is imported late, from main.py's own bottom-of-file section, for the
same reason the routes/integrations_*.py modules are: those names don't exist yet
earlier in main.py's execution. See routes/__init__.py's docstring for the full
mechanics and the import-order gotcha it creates for anything that imports this
module directly before main has finished loading.
"""

from __future__ import annotations

import collections
import re
import sqlite3
from datetime import datetime

from main import (
    _DEDUP_MIN_TITLE_WORDS,
    LOGGER,
    _bump_unread_counts_generation,
    _resolve_dedup_feed_urls,
    _safe_dedup_collect,
    _safe_dedup_find_pairs,
    author_filter_token,
    build_keyword_matcher,
    dedup_order_key,
    entry_effective_date,
    entry_url_slug,
    feed_display_title,
    get_reader,
    normalize_entry_title_for_dedupe,
    normalize_tag_value,
    parse_folders_scope_id,
    parse_tag_filter_spec,
    resolve_rule_feed_urls,
    title_word_similarity,
)
from services import html_sanitize


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
