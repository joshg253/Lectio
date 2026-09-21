"""Post-refresh automation rule execution: run-log writer, entry excerpting, and
keyword-rule matching, shared by every automation dispatcher (mark_as_read, dedup,
email/webhook/instapaper/quire/save_article/youtube_playlist).

Extracted out of main.py (Plan.md's main.py/index.html breakup, Step 2, Stage B).
This module does `from main import build_keyword_matcher` for the shared keyword-
matching primitive (staying in main.py -- see Plan.md's Step 4, `services/dedup.py`)
-- it is imported late, from main.py's own bottom-of-file section, for the same
reason the routes/integrations_*.py modules are: that name doesn't exist yet
earlier in main.py's execution. See routes/__init__.py's docstring for the full
mechanics and the import-order gotcha it creates for anything that imports this
module directly before main has finished loading.
"""

from __future__ import annotations

import re
import sqlite3

from main import build_keyword_matcher
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
