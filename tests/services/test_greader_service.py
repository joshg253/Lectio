"""Unit tests for the GReader-compatible API service."""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from services.greader import GReaderService


_USERNAME = "alice"
_PASSWORD = "hunter2"
_FEED_URL = "https://example.com/feed.xml"
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


def _mock_feed(url=_FEED_URL, title="Example Feed", link="https://example.com"):
    f = MagicMock()
    f.url = url
    f.title = title
    f.link = link
    f.updated = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return f


def _mock_entry(
    feed_url=_FEED_URL,
    entry_id=_ENTRY_ID,
    title="Test Entry",
    link="https://example.com/post",
    read=False,
    published=None,
    summary="<p>Content</p>",
    author="Author",
):
    e = MagicMock()
    e.feed_url = feed_url
    e.id = entry_id
    e.title = title
    e.link = link
    e.read = read
    e.published = published or datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)
    e.updated = e.published
    e.content = None
    e.summary = summary
    e.author = author
    return e


def _build_service(db_path: Path, reader_mock=None) -> GReaderService:
    if reader_mock is None:
        reader_mock = MagicMock()
        reader_mock.get_feeds.return_value = []
        reader_mock.get_entries.return_value = []

    def get_meta():
        return _make_meta(db_path)

    return GReaderService(
        get_meta_connection=get_meta,
        get_reader=lambda: reader_mock,
        username=_USERNAME,
        password=_PASSWORD,
        root_folder_name="All Feeds",
    )


def _seed_entry(db_path: Path, feed_url: str = _FEED_URL, entry_id: str = _ENTRY_ID) -> int:
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


# ------------------------------------------------------------------ auth

def test_authenticate_correct(tmp_path):
    svc = _build_service(tmp_path / "meta.sqlite")
    token = svc.authenticate(_USERNAME, _PASSWORD)
    assert token is not None and len(token) > 0


def test_authenticate_wrong_password(tmp_path):
    svc = _build_service(tmp_path / "meta.sqlite")
    assert svc.authenticate(_USERNAME, "wrongpass") is None


def test_authenticate_email_format(tmp_path):
    svc = _build_service(tmp_path / "meta.sqlite")
    token = svc.authenticate(f"{_USERNAME}@example.com", _PASSWORD)
    assert token is not None


def test_check_token_valid(tmp_path):
    svc = _build_service(tmp_path / "meta.sqlite")
    token = svc.authenticate(_USERNAME, _PASSWORD)
    assert token is not None
    assert svc.check_token(token) is True


def test_check_token_invalid(tmp_path):
    svc = _build_service(tmp_path / "meta.sqlite")
    assert svc.check_token("bogustoken") is False


# ------------------------------------------------------------------ ID parsing

def test_parse_item_id_decimal(tmp_path):
    svc = _build_service(tmp_path / "meta.sqlite")
    assert svc._parse_item_id("42") == 42


def test_parse_item_id_hex_prefix(tmp_path):
    svc = _build_service(tmp_path / "meta.sqlite")
    assert svc._parse_item_id("0x2a") == 42


def test_parse_item_id_tag_format(tmp_path):
    svc = _build_service(tmp_path / "meta.sqlite")
    assert svc._parse_item_id("tag:google.com,2005:reader/item/000000000000002a") == 42


def test_parse_item_id_invalid(tmp_path):
    svc = _build_service(tmp_path / "meta.sqlite")
    assert svc._parse_item_id("notanumber") is None


def test_format_item_id(tmp_path):
    svc = _build_service(tmp_path / "meta.sqlite")
    fmt = svc._format_item_id(42)
    assert fmt == "tag:google.com,2005:reader/item/000000000000002a"


# ------------------------------------------------------------------ user info

def test_get_user_info(tmp_path):
    svc = _build_service(tmp_path / "meta.sqlite")
    info = svc.get_user_info()
    assert info["userName"] == _USERNAME
    assert "userId" in info


# ------------------------------------------------------------------ tag list

def test_get_tag_list_default_states(tmp_path):
    svc = _build_service(tmp_path / "meta.sqlite")
    result = svc.get_tag_list()
    ids = [t["id"] for t in result["tags"]]
    assert "user/-/state/com.google/starred" in ids
    assert "user/-/state/com.google/kept-unread" in ids


def test_get_tag_list_includes_folders(tmp_path):
    db = tmp_path / "meta.sqlite"
    conn = _make_meta(db)
    conn.execute("INSERT INTO folders (name, parent_id) VALUES ('All Feeds', NULL)")
    conn.execute("INSERT INTO folders (name, parent_id) VALUES ('Tech', 1)")
    conn.commit()
    conn.close()

    svc = _build_service(db)
    result = svc.get_tag_list()
    ids = [t["id"] for t in result["tags"]]
    assert "user/-/label/Tech" in ids


def test_get_tag_list_excludes_system_folders(tmp_path):
    db = tmp_path / "meta.sqlite"
    conn = _make_meta(db)
    conn.execute("INSERT INTO folders (name, parent_id) VALUES ('All Feeds', NULL)")
    conn.execute("INSERT INTO folders (name, parent_id) VALUES ('_Lectio', 1)")
    conn.commit()
    conn.close()

    svc = _build_service(db)
    result = svc.get_tag_list()
    ids = [t["id"] for t in result["tags"]]
    assert "user/-/label/_Lectio" not in ids


# ------------------------------------------------------------------ subscription list

def test_get_subscription_list(tmp_path):
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

    result = svc.get_subscription_list()
    assert len(result["subscriptions"]) == 1
    sub = result["subscriptions"][0]
    assert sub["id"] == f"feed/{_FEED_URL}"
    assert sub["title"] == "Example Feed"
    assert any(c["label"] == "Tech" for c in sub["categories"])


# ------------------------------------------------------------------ unread counts

def test_get_unread_counts(tmp_path):
    db = tmp_path / "meta.sqlite"
    conn = _make_meta(db)
    conn.execute("INSERT INTO folders (name, parent_id) VALUES ('All Feeds', NULL)")
    conn.execute("INSERT INTO folders (name, parent_id) VALUES ('Tech', 1)")
    conn.execute("INSERT INTO folder_feeds (folder_id, feed_url) VALUES (2, ?)", (_FEED_URL,))
    conn.commit()
    conn.close()

    entry = _mock_entry(read=False)
    mock_reader = MagicMock()
    mock_reader.get_feeds.return_value = [_mock_feed()]
    mock_reader.get_entries.return_value = [entry]
    svc = _build_service(db, mock_reader)

    result = svc.get_unread_counts()
    ids = {u["id"]: u["count"] for u in result["unreadcounts"]}
    assert ids.get(f"feed/{_FEED_URL}", 0) == 1
    assert ids.get("user/-/label/Tech", 0) == 1
    assert ids.get("user/-/state/com.google/reading-list", 0) == 1


def test_get_unread_counts_empty(tmp_path):
    svc = _build_service(tmp_path / "meta.sqlite")
    result = svc.get_unread_counts()
    assert result["unreadcounts"] == []


# ------------------------------------------------------------------ stream item IDs

def test_get_stream_item_ids_reading_list(tmp_path):
    db = tmp_path / "meta.sqlite"
    entry = _mock_entry()
    mock_reader = MagicMock()
    mock_reader.get_feeds.return_value = []
    mock_reader.get_entries.return_value = [entry]
    svc = _build_service(db, mock_reader)

    result = svc.get_stream_item_ids("user/-/state/com.google/reading-list", count=10)

    assert len(result["itemRefs"]) == 1
    assert result["itemRefs"][0]["id"].isdigit()


def test_get_stream_item_ids_exclude_read(tmp_path):
    db = tmp_path / "meta.sqlite"
    unread = _mock_entry(read=False)
    read = _mock_entry(entry_id=_ENTRY_ID2, read=True)
    mock_reader = MagicMock()
    mock_reader.get_feeds.return_value = []
    mock_reader.get_entries.return_value = [unread]  # reader already filters when read=False passed
    svc = _build_service(db, mock_reader)

    result = svc.get_stream_item_ids(
        "user/-/state/com.google/reading-list", exclude_read=True
    )
    assert len(result["itemRefs"]) == 1


def test_get_stream_item_ids_continuation(tmp_path):
    db = tmp_path / "meta.sqlite"
    # Two entries with different timestamps.
    old_entry = _mock_entry(
        entry_id=_ENTRY_ID, published=datetime(2024, 1, 1, tzinfo=timezone.utc)
    )
    new_entry = _mock_entry(
        entry_id=_ENTRY_ID2, published=datetime(2024, 12, 1, tzinfo=timezone.utc)
    )
    mock_reader = MagicMock()
    mock_reader.get_feeds.return_value = []
    mock_reader.get_entries.return_value = [new_entry, old_entry]
    svc = _build_service(db, mock_reader)

    # First page: count=1 should return newest and set continuation.
    result = svc.get_stream_item_ids("user/-/state/com.google/reading-list", count=1)
    assert len(result["itemRefs"]) == 1
    assert "continuation" in result

    # Second page: use continuation to get older entry.
    result2 = svc.get_stream_item_ids(
        "user/-/state/com.google/reading-list", count=1, continuation=result["continuation"]
    )
    assert len(result2["itemRefs"]) == 1
    # Timestamps should be different entries.
    assert result["itemRefs"][0]["timestampUsec"] != result2["itemRefs"][0]["timestampUsec"]


# ------------------------------------------------------------------ item contents

def test_get_items_contents(tmp_path):
    db = tmp_path / "meta.sqlite"
    fever_id = _seed_entry(db)

    entry = _mock_entry()
    mock_reader = MagicMock()
    mock_reader.get_feeds.return_value = [_mock_feed()]
    mock_reader.get_entries.return_value = []
    mock_reader.get_entry.return_value = entry
    svc = _build_service(db, mock_reader)

    item_id = svc._format_item_id(fever_id)
    result = svc.get_items_contents([item_id])

    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["title"] == "Test Entry"
    assert "user/-/state/com.google/reading-list" in item["categories"]
    assert "user/-/state/com.google/read" not in item["categories"]


def test_get_items_contents_read_flag(tmp_path):
    db = tmp_path / "meta.sqlite"
    fever_id = _seed_entry(db)

    entry = _mock_entry(read=True)
    mock_reader = MagicMock()
    mock_reader.get_feeds.return_value = [_mock_feed()]
    mock_reader.get_entries.return_value = []
    mock_reader.get_entry.return_value = entry
    svc = _build_service(db, mock_reader)

    result = svc.get_items_contents([svc._format_item_id(fever_id)])
    assert "user/-/state/com.google/read" in result["items"][0]["categories"]


def test_get_items_contents_starred_flag(tmp_path):
    db = tmp_path / "meta.sqlite"
    fever_id = _seed_entry(db)
    conn = _make_meta(db)
    conn.execute("INSERT INTO saved_entries (feed_url, entry_id) VALUES (?, ?)", (_FEED_URL, _ENTRY_ID))
    conn.commit()
    conn.close()

    entry = _mock_entry()
    mock_reader = MagicMock()
    mock_reader.get_feeds.return_value = [_mock_feed()]
    mock_reader.get_entries.return_value = []
    mock_reader.get_entry.return_value = entry
    svc = _build_service(db, mock_reader)

    result = svc.get_items_contents([svc._format_item_id(fever_id)])
    assert "user/-/state/com.google/starred" in result["items"][0]["categories"]


# ------------------------------------------------------------------ edit tag

def test_edit_tag_mark_read(tmp_path):
    db = tmp_path / "meta.sqlite"
    fever_id = _seed_entry(db)

    mock_reader = MagicMock()
    mock_reader.get_feeds.return_value = []
    mock_reader.get_entries.return_value = []
    svc = _build_service(db, mock_reader)

    svc.edit_tag([str(fever_id)], ["user/-/state/com.google/read"], [])
    mock_reader.mark_entry_as_read.assert_called_once_with((_FEED_URL, _ENTRY_ID))


def test_edit_tag_mark_unread(tmp_path):
    db = tmp_path / "meta.sqlite"
    fever_id = _seed_entry(db)

    mock_reader = MagicMock()
    mock_reader.get_feeds.return_value = []
    mock_reader.get_entries.return_value = []
    svc = _build_service(db, mock_reader)

    svc.edit_tag([str(fever_id)], [], ["user/-/state/com.google/read"])
    mock_reader.mark_entry_as_unread.assert_called_once_with((_FEED_URL, _ENTRY_ID))


def test_edit_tag_star(tmp_path):
    db = tmp_path / "meta.sqlite"
    fever_id = _seed_entry(db)

    mock_reader = MagicMock()
    mock_reader.get_feeds.return_value = []
    mock_reader.get_entries.return_value = []
    svc = _build_service(db, mock_reader)

    svc.edit_tag([str(fever_id)], ["user/-/state/com.google/starred"], [])

    conn = _make_meta(db)
    row = conn.execute(
        "SELECT 1 FROM saved_entries WHERE feed_url=? AND entry_id=?", (_FEED_URL, _ENTRY_ID)
    ).fetchone()
    assert row is not None


def test_edit_tag_unstar(tmp_path):
    db = tmp_path / "meta.sqlite"
    fever_id = _seed_entry(db)
    conn = _make_meta(db)
    conn.execute("INSERT INTO saved_entries (feed_url, entry_id) VALUES (?, ?)", (_FEED_URL, _ENTRY_ID))
    conn.commit()
    conn.close()

    mock_reader = MagicMock()
    mock_reader.get_feeds.return_value = []
    mock_reader.get_entries.return_value = []
    svc = _build_service(db, mock_reader)

    svc.edit_tag([str(fever_id)], [], ["user/-/state/com.google/starred"])

    conn = _make_meta(db)
    row = conn.execute(
        "SELECT 1 FROM saved_entries WHERE feed_url=? AND entry_id=?", (_FEED_URL, _ENTRY_ID)
    ).fetchone()
    assert row is None


def test_edit_tag_tag_id_formats(tmp_path):
    """Decimal, hex, and full tag: URI formats must all resolve to the same entry."""
    db = tmp_path / "meta.sqlite"
    fever_id = _seed_entry(db)

    mock_reader = MagicMock()
    mock_reader.get_feeds.return_value = []
    mock_reader.get_entries.return_value = []
    svc = _build_service(db, mock_reader)

    for id_str in [
        str(fever_id),
        f"0x{fever_id:016x}",
        svc._format_item_id(fever_id),
    ]:
        mock_reader.reset_mock()
        svc.edit_tag([id_str], ["user/-/state/com.google/read"], [])
        mock_reader.mark_entry_as_read.assert_called_once()
