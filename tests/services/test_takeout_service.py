from __future__ import annotations

import io
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from services import takeout_service


# ---------------------------------------------------------------------------
# Helpers: create minimal in-memory DBs
# ---------------------------------------------------------------------------

def _make_meta_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE highlight_keywords (
            scope TEXT NOT NULL, scope_id TEXT NOT NULL, keyword TEXT NOT NULL,
            color TEXT NOT NULL DEFAULT 'yellow',
            PRIMARY KEY (scope, scope_id, keyword)
        );
        ALTER TABLE highlight_keywords ADD COLUMN is_regex INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE highlight_keywords ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1;
        ALTER TABLE highlight_keywords ADD COLUMN type TEXT NOT NULL DEFAULT 'highlight';
        ALTER TABLE highlight_keywords ADD COLUMN search_in TEXT NOT NULL DEFAULT 'title';
        ALTER TABLE highlight_keywords ADD COLUMN delivery TEXT NOT NULL DEFAULT 'immediately';
        ALTER TABLE highlight_keywords ADD COLUMN email_to TEXT NOT NULL DEFAULT '';
        ALTER TABLE highlight_keywords ADD COLUMN batch_time TEXT NOT NULL DEFAULT '';
        ALTER TABLE highlight_keywords ADD COLUMN batch_count INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE highlight_keywords ADD COLUMN cc_me INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE highlight_keywords ADD COLUMN dedup_window_hours INTEGER NOT NULL DEFAULT 24;
        ALTER TABLE highlight_keywords ADD COLUMN exclude_scope_ids TEXT NOT NULL DEFAULT '';
        ALTER TABLE highlight_keywords ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0;

        CREATE TABLE email_contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL DEFAULT '',
            address TEXT NOT NULL UNIQUE
        );

        CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT);

        CREATE TABLE saved_entries (
            feed_url TEXT NOT NULL,
            entry_id TEXT NOT NULL,
            saved_at TEXT NOT NULL,
            PRIMARY KEY (feed_url, entry_id)
        );

        CREATE TABLE read_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feed_url TEXT NOT NULL,
            entry_id TEXT NOT NULL,
            title TEXT,
            link TEXT,
            feed_title TEXT,
            read_at TEXT,
            UNIQUE(feed_url, entry_id)
        );
    """)
    return conn


def _make_reader_db(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "reader.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE entries (
            id TEXT NOT NULL, feed TEXT NOT NULL,
            title TEXT, link TEXT, published TEXT,
            PRIMARY KEY (id, feed)
        );
        CREATE TABLE entry_tags (
            id TEXT NOT NULL, feed TEXT NOT NULL,
            key TEXT NOT NULL, value TEXT,
            UNIQUE(id, feed, key)
        );
    """)
    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# Export tests
# ---------------------------------------------------------------------------

def test_export_produces_valid_zip(tmp_path):
    meta = _make_meta_db()
    rpath = _make_reader_db(tmp_path)
    data = takeout_service.build_takeout_zip(meta, rpath, "<opml/>", "1.0")
    assert data[:2] == b"PK"
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
    assert {"manifest.json", "opml.xml", "rules.json", "contacts.json",
            "settings.json", "tagged_entries.json", "starred_entries.json",
            "read_history.json"} <= names


def test_export_manifest_version(tmp_path):
    meta = _make_meta_db()
    rpath = _make_reader_db(tmp_path)
    data = takeout_service.build_takeout_zip(meta, rpath, "", "v42")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest["takeout_version"] == takeout_service.TAKEOUT_VERSION
    assert manifest["app_version"] == "v42"


def test_export_rules(tmp_path):
    meta = _make_meta_db()
    meta.execute(
        "INSERT INTO highlight_keywords (scope, scope_id, keyword, type)"
        " VALUES ('global', '', 'python', 'highlight')"
    )
    rpath = _make_reader_db(tmp_path)
    data = takeout_service.build_takeout_zip(meta, rpath, "", "x")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        rules = json.loads(zf.read("rules.json"))
    assert len(rules) == 1
    assert rules[0]["keyword"] == "python"


def test_export_skips_sensitive_settings(tmp_path):
    meta = _make_meta_db()
    meta.execute("INSERT INTO app_settings VALUES ('profile_name', 'Josh')")
    meta.execute("INSERT INTO app_settings VALUES ('instapaper_username', 'secret_user')")
    meta.execute("INSERT INTO app_settings VALUES ('instapaper_password', 'secret_pass')")
    rpath = _make_reader_db(tmp_path)
    data = takeout_service.build_takeout_zip(meta, rpath, "", "x")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        settings = json.loads(zf.read("settings.json"))
    assert "profile_name" in settings
    assert "instapaper_username" not in settings
    assert "instapaper_password" not in settings


def test_export_tagged_entries(tmp_path):
    meta = _make_meta_db()
    rpath = _make_reader_db(tmp_path)
    rconn = sqlite3.connect(str(rpath))
    rconn.execute("INSERT INTO entries VALUES ('e1','https://feed.example','My Post','https://post','2026-01-01')")
    rconn.execute("INSERT INTO entry_tags VALUES ('e1','https://feed.example','lectio.manual_tag.python','null')")
    rconn.commit()
    rconn.close()
    data = takeout_service.build_takeout_zip(meta, rpath, "", "x")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        tagged = json.loads(zf.read("tagged_entries.json"))
    assert len(tagged) == 1
    assert tagged[0]["entry_id"] == "e1"
    assert "python" in tagged[0]["tags"]
    assert tagged[0]["title"] == "My Post"


# ---------------------------------------------------------------------------
# Import tests
# ---------------------------------------------------------------------------

def _build_zip(**files) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        manifest = {"takeout_version": 1, "export_date": "2026-01-01T00:00:00Z", "app_version": "x"}
        zf.writestr("manifest.json", json.dumps(manifest))
        for name, content in files.items():
            zf.writestr(name, json.dumps(content) if not isinstance(content, str) else content)
    return buf.getvalue()


def test_import_rules(tmp_path):
    meta = _make_meta_db()
    rpath = _make_reader_db(tmp_path)
    rules = [{"scope": "global", "scope_id": "", "keyword": "rust", "type": "highlight",
               "color": "blue", "is_regex": 0, "enabled": 1, "search_in": "title",
               "delivery": "immediately", "email_to": "", "batch_time": "", "batch_count": 0,
               "cc_me": 0, "dedup_window_hours": 24, "exclude_scope_ids": "", "sort_order": 0}]
    zb = _build_zip(**{"rules.json": rules})
    with meta:
        summary = takeout_service.import_takeout_zip(meta, rpath, zb)
    assert summary["rules"] == 1
    row = meta.execute("SELECT * FROM highlight_keywords WHERE keyword='rust'").fetchone()
    assert row is not None


def test_import_rules_no_duplicate(tmp_path):
    meta = _make_meta_db()
    meta.execute("INSERT INTO highlight_keywords (scope, scope_id, keyword) VALUES ('global','','rust')")
    rpath = _make_reader_db(tmp_path)
    rules = [{"scope": "global", "scope_id": "", "keyword": "rust", "type": "highlight",
               "color": "yellow", "is_regex": 0, "enabled": 1, "search_in": "title",
               "delivery": "immediately", "email_to": "", "batch_time": "", "batch_count": 0,
               "cc_me": 0, "dedup_window_hours": 24, "exclude_scope_ids": "", "sort_order": 0}]
    zb = _build_zip(**{"rules.json": rules})
    with meta:
        summary = takeout_service.import_takeout_zip(meta, rpath, zb)
    assert summary["rules"] == 0
    count = meta.execute("SELECT COUNT(*) FROM highlight_keywords WHERE keyword='rust'").fetchone()[0]
    assert count == 1


def test_import_contacts(tmp_path):
    meta = _make_meta_db()
    rpath = _make_reader_db(tmp_path)
    contacts = [{"label": "Alice", "address": "alice@example.com"}]
    zb = _build_zip(**{"contacts.json": contacts})
    with meta:
        summary = takeout_service.import_takeout_zip(meta, rpath, zb)
    assert summary["contacts"] == 1
    row = meta.execute("SELECT * FROM email_contacts WHERE address='alice@example.com'").fetchone()
    assert row["label"] == "Alice"


def test_import_contacts_no_duplicate(tmp_path):
    meta = _make_meta_db()
    meta.execute("INSERT INTO email_contacts (label, address) VALUES ('Alice','alice@example.com')")
    rpath = _make_reader_db(tmp_path)
    contacts = [{"label": "Alice", "address": "alice@example.com"}]
    zb = _build_zip(**{"contacts.json": contacts})
    with meta:
        summary = takeout_service.import_takeout_zip(meta, rpath, zb)
    assert summary["contacts"] == 0


def test_import_history_appends(tmp_path):
    meta = _make_meta_db()
    rpath = _make_reader_db(tmp_path)
    history = [
        {"feed_url": "https://f.example/rss", "entry_id": "e1",
         "title": "A Post", "link": "https://post", "feed_title": "F", "read_at": "2026-01-01"},
    ]
    zb = _build_zip(**{"read_history.json": history})
    with meta:
        summary = takeout_service.import_takeout_zip(meta, rpath, zb)
    assert summary["read_history"] == 1
    row = meta.execute("SELECT title FROM read_history WHERE entry_id='e1'").fetchone()
    assert row[0] == "A Post"


def test_import_tagged_entries(tmp_path):
    meta = _make_meta_db()
    rpath = _make_reader_db(tmp_path)
    rconn = sqlite3.connect(str(rpath))
    rconn.execute("INSERT INTO entries VALUES ('e1','https://f.example/rss','Post','https://p','2026-01-01')")
    rconn.commit()
    rconn.close()
    tagged = [{"feed_url": "https://f.example/rss", "entry_id": "e1", "tags": ["rust", "cpp"]}]
    zb = _build_zip(**{"tagged_entries.json": tagged})
    with meta:
        summary = takeout_service.import_takeout_zip(meta, rpath, zb)
    assert summary["tagged_entries"] == 1
    rconn = sqlite3.connect(str(rpath))
    tags = [r[0] for r in rconn.execute(
        "SELECT key FROM entry_tags WHERE id='e1'"
    ).fetchall()]
    assert "lectio.manual_tag.rust" in tags
    assert "lectio.manual_tag.cpp" in tags
    rconn.close()


def test_import_tagged_skips_missing_entry(tmp_path):
    meta = _make_meta_db()
    rpath = _make_reader_db(tmp_path)
    tagged = [{"feed_url": "https://gone.example/rss", "entry_id": "missing", "tags": ["rust"]}]
    zb = _build_zip(**{"tagged_entries.json": tagged})
    with meta:
        summary = takeout_service.import_takeout_zip(meta, rpath, zb)
    assert summary["tagged_entries"] == 0


def test_import_starred(tmp_path):
    meta = _make_meta_db()
    rpath = _make_reader_db(tmp_path)
    rconn = sqlite3.connect(str(rpath))
    rconn.execute("INSERT INTO entries VALUES ('e1','https://f.example/rss','Star','https://s','2026-01-01')")
    rconn.commit()
    rconn.close()
    starred = [{"feed_url": "https://f.example/rss", "entry_id": "e1", "saved_at": "2026-01-02"}]
    zb = _build_zip(**{"starred_entries.json": starred})
    with meta:
        summary = takeout_service.import_takeout_zip(meta, rpath, zb)
    assert summary["starred_entries"] == 1
    row = meta.execute("SELECT saved_at FROM saved_entries WHERE entry_id='e1'").fetchone()
    assert row[0] == "2026-01-02"


def test_import_rejects_future_version(tmp_path):
    meta = _make_meta_db()
    rpath = _make_reader_db(tmp_path)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"takeout_version": 9999}))
    with pytest.raises(ValueError, match="newer"):
        takeout_service.import_takeout_zip(meta, rpath, buf.getvalue())


def test_roundtrip(tmp_path):
    meta = _make_meta_db()
    meta.execute("INSERT INTO highlight_keywords (scope, scope_id, keyword, type) VALUES ('global','','roundtrip','highlight')")
    meta.execute("INSERT INTO email_contacts (label, address) VALUES ('Bob','bob@example.com')")
    meta.execute("INSERT INTO app_settings VALUES ('profile_name', 'Test User')")
    meta.execute("INSERT INTO read_history (feed_url, entry_id, title, link, feed_title, read_at) VALUES ('https://f.example/rss','e1','T','https://l','F','2026-01-01')")
    rpath = _make_reader_db(tmp_path)

    data = takeout_service.build_takeout_zip(meta, rpath, "<opml/>", "test")

    meta2 = _make_meta_db()
    rpath2 = _make_reader_db(tmp_path / "r2")
    with meta2:
        summary = takeout_service.import_takeout_zip(meta2, rpath2, data)

    assert summary["rules"] == 1
    assert summary["contacts"] == 1
    assert summary["read_history"] == 1
    assert meta2.execute("SELECT keyword FROM highlight_keywords").fetchone()[0] == "roundtrip"
    assert meta2.execute("SELECT address FROM email_contacts").fetchone()[0] == "bob@example.com"
