"""Unit tests for the Fever API service."""
from __future__ import annotations

import hashlib
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from services.fever import FeverService


_USERNAME = "alice"
_FEVER_PASS = "hunter2"
_FEVER_KEY = hashlib.md5(f"{_USERNAME}:{_FEVER_PASS}".encode()).hexdigest()

_FEED_URL = "https://example.com/feed.xml"
_FEED_URL2 = "https://other.com/feed.xml"
_ENTRY_ID = "eid-001"
_ENTRY_ID2 = "eid-002"


def _make_meta(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            parent_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS folder_feeds (
            folder_id INTEGER NOT NULL,
            feed_url TEXT NOT NULL,
            PRIMARY KEY(folder_id, feed_url)
        );
        CREATE TABLE IF NOT EXISTS saved_entries (
            feed_url TEXT NOT NULL,
            entry_id TEXT NOT NULL,
            PRIMARY KEY(feed_url, entry_id)
        );
        CREATE TABLE IF NOT EXISTS fever_feed_map (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feed_url TEXT UNIQUE NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fever_group_map (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT UNIQUE NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fever_entry_map (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feed_url TEXT NOT NULL,
            entry_id TEXT NOT NULL,
            UNIQUE(feed_url, entry_id)
        );
        CREATE INDEX IF NOT EXISTS idx_fever_entry_map_feed ON fever_entry_map(feed_url);
        """
    )
    return conn


def _mock_feed(url=_FEED_URL, title="Example Feed", link="https://example.com", updated=None):
    f = MagicMock()
    f.url = url
    f.title = title
    f.link = link
    f.updated = updated or datetime(2024, 1, 1, tzinfo=timezone.utc)
    return f


def _mock_entry(
    feed_url=_FEED_URL,
    entry_id=_ENTRY_ID,
    title="Test Entry",
    link="https://example.com/post",
    read=False,
    published=None,
    content=None,
    summary=None,
    author="",
):
    e = MagicMock()
    e.feed_url = feed_url
    e.id = entry_id
    e.title = title
    e.link = link
    e.read = read
    e.published = published or datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    e.updated = e.published
    e.content = content  # None or list with .value
    e.summary = summary
    e.author = author
    return e


def _build_service(db_path: Path, reader_mock=None) -> FeverService:
    if reader_mock is None:
        reader_mock = MagicMock()
        reader_mock.get_feeds.return_value = []
        reader_mock.get_entries.return_value = []

    def get_meta():
        return _make_meta(db_path)

    svc = FeverService(
        get_meta_connection=get_meta,
        get_reader=lambda: reader_mock,
        fever_api_key=_FEVER_KEY,
        root_folder_name="All Feeds",
    )
    # Cancel background pre-sync to keep tests deterministic.
    import threading
    for t in threading.enumerate():
        if t.daemon and t.name != "MainThread":
            t.join(timeout=0.5)
    return svc


# ------------------------------------------------------------------ auth

def test_check_auth_correct(tmp_path):
    svc = _build_service(tmp_path / "meta.sqlite")
    assert svc.check_auth(_FEVER_KEY) is True


def test_check_auth_wrong_key(tmp_path):
    svc = _build_service(tmp_path / "meta.sqlite")
    assert svc.check_auth("deadbeef" * 4) is False


def test_check_auth_case_insensitive(tmp_path):
    svc = _build_service(tmp_path / "meta.sqlite")
    assert svc.check_auth(_FEVER_KEY.upper()) is True


# ------------------------------------------------------------------ feeds & groups

def test_get_feeds_and_groups_empty(tmp_path):
    svc = _build_service(tmp_path / "meta.sqlite")
    data = svc.get_feeds_and_groups()
    assert data["feeds"] == []
    assert data["groups"] == []
    assert data["feeds_groups"] == []


def test_get_feeds_returns_feed_list(tmp_path):
    db = tmp_path / "meta.sqlite"
    mock_reader = MagicMock()
    mock_reader.get_feeds.return_value = [_mock_feed()]
    mock_reader.get_entries.return_value = []
    svc = _build_service(db, mock_reader)

    data = svc.get_feeds_and_groups()

    assert len(data["feeds"]) == 1
    feed = data["feeds"][0]
    assert feed["url"] == _FEED_URL
    assert feed["title"] == "Example Feed"
    assert isinstance(feed["id"], int)
    assert feed["is_spark"] == 0
    assert feed["favicon_id"] == 0


def test_get_groups_with_folder(tmp_path):
    db = tmp_path / "meta.sqlite"
    conn = _make_meta(db)
    conn.execute("INSERT INTO folders (name, parent_id) VALUES ('All Feeds', NULL)")
    conn.execute("INSERT INTO folders (name, parent_id) VALUES ('Tech', 1)")
    conn.execute("INSERT INTO folder_feeds (folder_id, feed_url) VALUES (2, ?)", (_FEED_URL,))
    conn.commit()
    conn.close()

    mock_reader = MagicMock()
    mock_reader.get_feeds.return_value = [_mock_feed()]
    mock_reader.get_entries.return_value = []
    svc = _build_service(db, mock_reader)

    data = svc.get_feeds_and_groups()

    assert len(data["groups"]) == 1
    assert data["groups"][0]["title"] == "Tech"
    assert len(data["feeds_groups"]) == 1
    group_id = data["groups"][0]["id"]
    fg = data["feeds_groups"][0]
    assert fg["group_id"] == group_id


def test_system_folders_excluded_from_groups(tmp_path):
    db = tmp_path / "meta.sqlite"
    conn = _make_meta(db)
    conn.execute("INSERT INTO folders (name, parent_id) VALUES ('All Feeds', NULL)")
    conn.execute("INSERT INTO folders (name, parent_id) VALUES ('_Lectio', 1)")
    conn.commit()
    conn.close()

    mock_reader = MagicMock()
    mock_reader.get_feeds.return_value = []
    mock_reader.get_entries.return_value = []
    svc = _build_service(db, mock_reader)

    data = svc.get_feeds_and_groups()
    assert data["groups"] == []


# ------------------------------------------------------------------ items

def _seed_entry_map(db_path: Path, feed_url: str, entry_id: str) -> int:
    """Insert an entry into fever_entry_map and return its integer ID."""
    conn = _make_meta(db_path)
    conn.execute(
        "INSERT OR IGNORE INTO fever_entry_map (feed_url, entry_id) VALUES (?, ?)",
        (feed_url, entry_id),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM fever_entry_map WHERE feed_url=? AND entry_id=?",
        (feed_url, entry_id),
    ).fetchone()
    conn.close()
    return row["id"]


def test_get_items_returns_entry(tmp_path):
    db = tmp_path / "meta.sqlite"
    item_id = _seed_entry_map(db, _FEED_URL, _ENTRY_ID)

    entry = _mock_entry()
    mock_reader = MagicMock()
    mock_reader.get_feeds.return_value = [_mock_feed()]
    mock_reader.get_entries.return_value = [entry]
    mock_reader.get_entry.return_value = entry
    svc = _build_service(db, mock_reader)
    svc._synced = True  # skip background sync

    result = svc.get_items()

    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["id"] == item_id
    assert item["title"] == "Test Entry"
    assert item["is_read"] == 0
    assert item["is_saved"] == 0


def test_get_items_since_id(tmp_path):
    db = tmp_path / "meta.sqlite"
    id1 = _seed_entry_map(db, _FEED_URL, _ENTRY_ID)
    id2 = _seed_entry_map(db, _FEED_URL, _ENTRY_ID2)

    entry1 = _mock_entry(entry_id=_ENTRY_ID)
    entry2 = _mock_entry(entry_id=_ENTRY_ID2)
    mock_reader = MagicMock()
    mock_reader.get_feeds.return_value = []
    mock_reader.get_entries.return_value = []
    mock_reader.get_entry.side_effect = lambda key, default=None: (
        entry1 if key[1] == _ENTRY_ID else entry2
    )
    svc = _build_service(db, mock_reader)
    svc._synced = True

    result = svc.get_items(since_id=id1)

    returned_ids = [item["id"] for item in result["items"]]
    assert id2 in returned_ids
    assert id1 not in returned_ids


def test_get_items_with_ids(tmp_path):
    db = tmp_path / "meta.sqlite"
    id1 = _seed_entry_map(db, _FEED_URL, _ENTRY_ID)
    _seed_entry_map(db, _FEED_URL, _ENTRY_ID2)

    entry1 = _mock_entry(entry_id=_ENTRY_ID)
    mock_reader = MagicMock()
    mock_reader.get_feeds.return_value = []
    mock_reader.get_entries.return_value = []
    mock_reader.get_entry.return_value = entry1
    svc = _build_service(db, mock_reader)
    svc._synced = True

    result = svc.get_items(with_ids=str(id1))

    assert len(result["items"]) == 1
    assert result["items"][0]["id"] == id1


def test_get_items_is_read_flag(tmp_path):
    db = tmp_path / "meta.sqlite"
    _seed_entry_map(db, _FEED_URL, _ENTRY_ID)

    entry = _mock_entry(read=True)
    mock_reader = MagicMock()
    mock_reader.get_feeds.return_value = []
    mock_reader.get_entries.return_value = []
    mock_reader.get_entry.return_value = entry
    svc = _build_service(db, mock_reader)
    svc._synced = True

    result = svc.get_items()
    assert result["items"][0]["is_read"] == 1


def test_get_items_is_saved_flag(tmp_path):
    db = tmp_path / "meta.sqlite"
    _seed_entry_map(db, _FEED_URL, _ENTRY_ID)
    conn = _make_meta(db)
    conn.execute("INSERT INTO saved_entries (feed_url, entry_id) VALUES (?, ?)", (_FEED_URL, _ENTRY_ID))
    conn.commit()
    conn.close()

    entry = _mock_entry()
    mock_reader = MagicMock()
    mock_reader.get_feeds.return_value = []
    mock_reader.get_entries.return_value = []
    mock_reader.get_entry.return_value = entry
    svc = _build_service(db, mock_reader)
    svc._synced = True

    result = svc.get_items()
    assert result["items"][0]["is_saved"] == 1


# ------------------------------------------------------------------ unread / saved IDs

def test_get_unread_item_ids(tmp_path):
    db = tmp_path / "meta.sqlite"
    entry = _mock_entry(read=False)
    mock_reader = MagicMock()
    mock_reader.get_feeds.return_value = []
    mock_reader.get_entries.return_value = [entry]
    svc = _build_service(db, mock_reader)
    svc._synced = True

    ids_str = svc.get_unread_item_ids()

    assert ids_str != ""
    ids = [int(x) for x in ids_str.split(",")]
    assert len(ids) == 1


def test_get_unread_item_ids_empty(tmp_path):
    db = tmp_path / "meta.sqlite"
    mock_reader = MagicMock()
    mock_reader.get_feeds.return_value = []
    mock_reader.get_entries.return_value = []
    svc = _build_service(db, mock_reader)
    svc._synced = True

    assert svc.get_unread_item_ids() == ""


def test_get_saved_item_ids(tmp_path):
    db = tmp_path / "meta.sqlite"
    _seed_entry_map(db, _FEED_URL, _ENTRY_ID)
    conn = _make_meta(db)
    conn.execute("INSERT INTO saved_entries (feed_url, entry_id) VALUES (?, ?)", (_FEED_URL, _ENTRY_ID))
    conn.commit()
    conn.close()

    mock_reader = MagicMock()
    mock_reader.get_feeds.return_value = []
    mock_reader.get_entries.return_value = []
    svc = _build_service(db, mock_reader)
    svc._synced = True

    ids_str = svc.get_saved_item_ids()
    assert ids_str != ""
    assert len(ids_str.split(",")) == 1


# ------------------------------------------------------------------ mark actions

def test_mark_item_read(tmp_path):
    db = tmp_path / "meta.sqlite"
    item_id = _seed_entry_map(db, _FEED_URL, _ENTRY_ID)

    mock_reader = MagicMock()
    mock_reader.get_feeds.return_value = []
    mock_reader.get_entries.return_value = []
    svc = _build_service(db, mock_reader)
    svc._synced = True

    svc.mark_item(item_id, "read")
    mock_reader.mark_entry_as_read.assert_called_once_with((_FEED_URL, _ENTRY_ID))


def test_mark_item_unread(tmp_path):
    db = tmp_path / "meta.sqlite"
    item_id = _seed_entry_map(db, _FEED_URL, _ENTRY_ID)

    mock_reader = MagicMock()
    mock_reader.get_feeds.return_value = []
    mock_reader.get_entries.return_value = []
    svc = _build_service(db, mock_reader)
    svc._synced = True

    svc.mark_item(item_id, "unread")
    mock_reader.mark_entry_as_unread.assert_called_once_with((_FEED_URL, _ENTRY_ID))


def test_mark_item_saved(tmp_path):
    db = tmp_path / "meta.sqlite"
    item_id = _seed_entry_map(db, _FEED_URL, _ENTRY_ID)

    mock_reader = MagicMock()
    mock_reader.get_feeds.return_value = []
    mock_reader.get_entries.return_value = []
    svc = _build_service(db, mock_reader)
    svc._synced = True

    svc.mark_item(item_id, "saved")

    conn = _make_meta(db)
    row = conn.execute("SELECT 1 FROM saved_entries WHERE feed_url=? AND entry_id=?", (_FEED_URL, _ENTRY_ID)).fetchone()
    assert row is not None


def test_mark_item_unsaved(tmp_path):
    db = tmp_path / "meta.sqlite"
    item_id = _seed_entry_map(db, _FEED_URL, _ENTRY_ID)
    conn = _make_meta(db)
    conn.execute("INSERT INTO saved_entries (feed_url, entry_id) VALUES (?, ?)", (_FEED_URL, _ENTRY_ID))
    conn.commit()
    conn.close()

    mock_reader = MagicMock()
    mock_reader.get_feeds.return_value = []
    mock_reader.get_entries.return_value = []
    svc = _build_service(db, mock_reader)
    svc._synced = True

    svc.mark_item(item_id, "unsaved")

    conn = _make_meta(db)
    row = conn.execute("SELECT 1 FROM saved_entries WHERE feed_url=? AND entry_id=?", (_FEED_URL, _ENTRY_ID)).fetchone()
    assert row is None


def test_mark_item_unknown_id_is_noop(tmp_path):
    db = tmp_path / "meta.sqlite"
    mock_reader = MagicMock()
    mock_reader.get_feeds.return_value = []
    mock_reader.get_entries.return_value = []
    svc = _build_service(db, mock_reader)
    svc._synced = True

    svc.mark_item(99999, "read")  # should not raise
    mock_reader.mark_entry_as_read.assert_not_called()


def test_mark_feed_read_before_cutoff(tmp_path):
    db = tmp_path / "meta.sqlite"
    conn = _make_meta(db)
    conn.execute("INSERT INTO fever_feed_map (feed_url) VALUES (?)", (_FEED_URL,))
    conn.commit()
    feed_id = conn.execute("SELECT id FROM fever_feed_map WHERE feed_url=?", (_FEED_URL,)).fetchone()["id"]
    conn.close()

    cutoff_dt = datetime(2024, 6, 1, tzinfo=timezone.utc)
    old_entry = _mock_entry(published=datetime(2024, 1, 1, tzinfo=timezone.utc), read=False)
    new_entry = _mock_entry(entry_id=_ENTRY_ID2, published=datetime(2024, 12, 1, tzinfo=timezone.utc), read=False)

    mock_reader = MagicMock()
    mock_reader.get_feeds.return_value = []
    mock_reader.get_entries.return_value = [old_entry, new_entry]
    svc = _build_service(db, mock_reader)
    svc._synced = True

    svc.mark_feed_read(feed_id, int(cutoff_dt.timestamp()))

    # Only old_entry should be marked read.
    calls = [c.args[0] for c in mock_reader.mark_entry_as_read.call_args_list]
    assert (_FEED_URL, _ENTRY_ID) in calls
    assert (_FEED_URL, _ENTRY_ID2) not in calls
