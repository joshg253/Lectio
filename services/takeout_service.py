"""Takeout: export all user data as a ZIP and import it back.

Export ZIP layout:
  manifest.json         — version, date, lectio_version
  opml.xml              — feed/folder structure (existing OPML export)
  rules.json            — automation rules (highlight_keywords table)
  contacts.json         — email contacts
  settings.json         — app_settings (sensitive credentials omitted)
  tagged_entries.json   — entries that have manual tags
  starred_entries.json  — saved/starred entries
  read_history.json     — read history log

Import merges data non-destructively:
  rules     — skips exact duplicates (scope+keyword+type)
  contacts  — skips duplicate addresses
  settings  — skips sensitive/ephemeral keys
  tagged    — re-applies tags via reader API if entry exists
  starred   — inserts into saved_entries if entry exists in reader
  history   — appends rows (INSERT OR IGNORE on feed_url+entry_id)
"""
from __future__ import annotations

import io
import json
import logging
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path

LOGGER = logging.getLogger(__name__)

TAKEOUT_VERSION = 1

_SETTINGS_SKIP_EXPORT = frozenset({
    "instapaper_username",
    "instapaper_password",
})

_SETTINGS_SKIP_IMPORT = frozenset({
    "instapaper_username",
    "instapaper_password",
    "problematic_feeds_last_viewed_at",
    "youtube_sync_last_at",
    "youtube_sync_last_result",
    "maintenance_last_ran_at",
})

_TAG_PREFIX = "lectio.manual_tag."


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def build_takeout_zip(
    meta_conn: sqlite3.Connection,
    reader_db_path: Path,
    opml_text: str,
    app_version: str = "unknown",
) -> bytes:
    """Build and return the raw bytes of the takeout ZIP."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps({
            "takeout_version": TAKEOUT_VERSION,
            "export_date": datetime.now(timezone.utc).isoformat(),
            "app_version": app_version,
        }, indent=2))

        zf.writestr("opml.xml", opml_text)
        zf.writestr("rules.json", json.dumps(_export_rules(meta_conn), indent=2))
        zf.writestr("contacts.json", json.dumps(_export_contacts(meta_conn), indent=2))
        zf.writestr("settings.json", json.dumps(_export_settings(meta_conn), indent=2))

        reader_conn = sqlite3.connect(str(reader_db_path), uri=False, check_same_thread=False)
        reader_conn.row_factory = sqlite3.Row
        try:
            zf.writestr("tagged_entries.json", json.dumps(_export_tagged(reader_conn), indent=2))
            zf.writestr("starred_entries.json", json.dumps(_export_starred(meta_conn, reader_conn), indent=2))
        finally:
            reader_conn.close()

        zf.writestr("read_history.json", json.dumps(_export_history(meta_conn), indent=2))

    return buf.getvalue()


def _export_rules(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT scope, scope_id, keyword, type, color, is_regex, enabled,"
        "  search_in, delivery, email_to, batch_time, batch_count, cc_me,"
        "  dedup_window_hours, exclude_scope_ids, sort_order"
        " FROM highlight_keywords ORDER BY sort_order, rowid"
    ).fetchall()
    return [dict(r) for r in rows]


def _export_contacts(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT label, address FROM email_contacts ORDER BY rowid").fetchall()
    return [dict(r) for r in rows]


def _export_settings(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
    return {
        r["key"]: r["value"]
        for r in rows
        if r["key"] not in _SETTINGS_SKIP_EXPORT
    }


def _export_tagged(reader_conn: sqlite3.Connection) -> list[dict]:
    tag_rows = reader_conn.execute(
        "SELECT id, feed, key FROM entry_tags WHERE key LIKE ?",
        (f"{_TAG_PREFIX}%",),
    ).fetchall()

    entries: dict[tuple, dict] = {}
    for row in tag_rows:
        key = (str(row["feed"]), str(row["id"]))
        tag_name = row["key"][len(_TAG_PREFIX):]
        if key not in entries:
            entries[key] = {"feed_url": key[0], "entry_id": key[1], "tags": []}
        entries[key]["tags"].append(tag_name)

    if not entries:
        return []

    entry_keys = list(entries.keys())
    entry_meta = _bulk_fetch_entry_meta(reader_conn, entry_keys)
    for key, meta in entry_meta.items():
        if key in entries:
            entries[key].update(meta)

    return sorted(entries.values(), key=lambda e: e.get("published") or "")


def _export_starred(meta_conn: sqlite3.Connection, reader_conn: sqlite3.Connection) -> list[dict]:
    rows = meta_conn.execute(
        "SELECT feed_url, entry_id, saved_at FROM saved_entries ORDER BY saved_at DESC"
    ).fetchall()
    if not rows:
        return []

    entry_keys = [(r["feed_url"], r["entry_id"]) for r in rows]
    saved_at = {(r["feed_url"], r["entry_id"]): r["saved_at"] for r in rows}
    entry_meta = _bulk_fetch_entry_meta(reader_conn, entry_keys)

    result = []
    for key in entry_keys:
        item = {"feed_url": key[0], "entry_id": key[1], "saved_at": saved_at[key]}
        item.update(entry_meta.get(key, {}))
        result.append(item)
    return result


def _export_history(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT feed_url, entry_id, title, link, feed_title, read_at"
        " FROM read_history ORDER BY read_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def _bulk_fetch_entry_meta(
    reader_conn: sqlite3.Connection,
    entry_keys: list[tuple[str, str]],
) -> dict[tuple[str, str], dict]:
    """Fetch title, link, published for a list of (feed_url, entry_id) pairs."""
    if not entry_keys:
        return {}
    placeholders = ",".join("(?,?)" for _ in entry_keys)
    flat = [v for pair in entry_keys for v in pair]
    rows = reader_conn.execute(
        f"SELECT feed, id, title, link, published FROM entries"
        f" WHERE (feed, id) IN ({placeholders})",
        flat,
    ).fetchall()
    result: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (str(row["feed"]), str(row["id"]))
        result[key] = {
            "title": row["title"],
            "link": row["link"],
            "published": str(row["published"]) if row["published"] else None,
        }
    return result


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def import_takeout_zip(
    meta_conn: sqlite3.Connection,
    reader_db_path: Path,
    zip_bytes: bytes,
) -> dict:
    """Import a takeout ZIP. Returns a summary dict with counts."""
    summary: dict[str, int] = {}

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = set(zf.namelist())

        def _load(name: str):
            if name not in names:
                return None
            data = zf.read(name)
            if name.endswith(".json"):
                return json.loads(data)
            return data

        manifest = _load("manifest.json") or {}
        version = manifest.get("takeout_version", 1)
        if version > TAKEOUT_VERSION:
            raise ValueError(f"Takeout version {version} is newer than this Lectio supports ({TAKEOUT_VERSION})")

        if "rules.json" in names:
            summary["rules"] = _import_rules(meta_conn, _load("rules.json") or [])

        if "contacts.json" in names:
            summary["contacts"] = _import_contacts(meta_conn, _load("contacts.json") or [])

        if "settings.json" in names:
            summary["settings"] = _import_settings(meta_conn, _load("settings.json") or {})

        reader_conn = sqlite3.connect(str(reader_db_path), uri=False, check_same_thread=False)
        reader_conn.row_factory = sqlite3.Row
        reader_conn.isolation_level = None
        try:
            if "tagged_entries.json" in names:
                summary["tagged_entries"] = _import_tagged(reader_conn, _load("tagged_entries.json") or [])

            if "starred_entries.json" in names:
                summary["starred_entries"] = _import_starred(
                    meta_conn, reader_conn, _load("starred_entries.json") or []
                )
        finally:
            reader_conn.close()

        if "read_history.json" in names:
            summary["read_history"] = _import_history(meta_conn, _load("read_history.json") or [])

    LOGGER.info("[takeout] import complete: %s", summary)
    return summary


def _import_rules(conn: sqlite3.Connection, rules: list[dict]) -> int:
    inserted = 0
    for r in rules:
        try:
            conn.execute(
                "INSERT OR IGNORE INTO highlight_keywords"
                " (scope, scope_id, keyword, type, color, is_regex, enabled,"
                "  search_in, delivery, email_to, batch_time, batch_count, cc_me,"
                "  dedup_window_hours, exclude_scope_ids, sort_order)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    r.get("scope", "global"), r.get("scope_id", ""),
                    r.get("keyword", ""), r.get("type", "highlight"),
                    r.get("color", "yellow"), int(r.get("is_regex", 0)),
                    int(r.get("enabled", 1)), r.get("search_in", "title"),
                    r.get("delivery", "immediately"), r.get("email_to", ""),
                    r.get("batch_time", ""), int(r.get("batch_count", 0)),
                    int(r.get("cc_me", 0)), int(r.get("dedup_window_hours", 24)),
                    r.get("exclude_scope_ids", ""), int(r.get("sort_order", 0)),
                ),
            )
            inserted += conn.execute("SELECT changes()").fetchone()[0]
        except Exception:
            LOGGER.warning("[takeout] skipping rule %s: import error", r.get("keyword"), exc_info=True)
    return inserted


def _import_contacts(conn: sqlite3.Connection, contacts: list[dict]) -> int:
    inserted = 0
    for c in contacts:
        addr = str(c.get("address") or "").strip()
        if not addr:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO email_contacts (label, address) VALUES (?, ?)",
            (c.get("label", ""), addr),
        )
        inserted += conn.execute("SELECT changes()").fetchone()[0]
    return inserted


def _import_settings(conn: sqlite3.Connection, settings: dict) -> int:
    updated = 0
    for key, value in settings.items():
        if key in _SETTINGS_SKIP_IMPORT:
            continue
        existing = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        if existing is None:
            conn.execute("INSERT INTO app_settings (key, value) VALUES (?, ?)", (key, value))
            updated += 1
    return updated


def _import_tagged(reader_conn: sqlite3.Connection, entries: list[dict]) -> int:
    applied = 0
    for entry in entries:
        feed_url = str(entry.get("feed_url") or "")
        entry_id = str(entry.get("entry_id") or "")
        tags = entry.get("tags") or []
        if not feed_url or not entry_id or not tags:
            continue
        exists = reader_conn.execute(
            "SELECT 1 FROM entries WHERE feed = ? AND id = ?", (feed_url, entry_id)
        ).fetchone()
        if not exists:
            continue
        for tag in tags:
            tag_key = f"{_TAG_PREFIX}{tag}"
            reader_conn.execute(
                "INSERT OR IGNORE INTO entry_tags (feed, id, key, value) VALUES (?, ?, ?, 'null')",
                (feed_url, entry_id, tag_key),
            )
        applied += 1
    return applied


def _import_starred(
    meta_conn: sqlite3.Connection,
    reader_conn: sqlite3.Connection,
    entries: list[dict],
) -> int:
    inserted = 0
    now = datetime.now(timezone.utc).isoformat()
    for entry in entries:
        feed_url = str(entry.get("feed_url") or "")
        entry_id = str(entry.get("entry_id") or "")
        if not feed_url or not entry_id:
            continue
        exists = reader_conn.execute(
            "SELECT 1 FROM entries WHERE feed = ? AND id = ?", (feed_url, entry_id)
        ).fetchone()
        if not exists:
            continue
        saved_at = entry.get("saved_at") or now
        meta_conn.execute(
            "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id, saved_at) VALUES (?, ?, ?)",
            (feed_url, entry_id, saved_at),
        )
        inserted += meta_conn.execute("SELECT changes()").fetchone()[0]
    return inserted


def _import_history(conn: sqlite3.Connection, history: list[dict]) -> int:
    inserted = 0
    for row in history:
        feed_url = str(row.get("feed_url") or "")
        entry_id = str(row.get("entry_id") or "")
        if not feed_url or not entry_id:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO read_history"
            " (feed_url, entry_id, title, link, feed_title, read_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                feed_url, entry_id,
                row.get("title", ""), row.get("link", ""),
                row.get("feed_title", ""), row.get("read_at", ""),
            ),
        )
        inserted += conn.execute("SELECT changes()").fetchone()[0]
    return inserted
