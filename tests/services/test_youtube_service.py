from __future__ import annotations

import sqlite3
from pathlib import Path

from services.youtube import YouTubeDurationService


class _ReaderCtx:
    def __init__(self, reader):
        self._reader = reader

    def __enter__(self):
        return self._reader

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeEntry:
    def __init__(self, link: str):
        self.link = link


class _FakeReader:
    def __init__(self, entries):
        self._entries = entries

    def get_entries(self, feed: str, limit: int = 50):
        return list(self._entries)


def _make_db_conn(db_path: Path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS youtube_video_duration (
            video_id TEXT PRIMARY KEY,
            duration_seconds INTEGER,
            duration_display TEXT,
            fetched_at TEXT
        )
        """
    )
    return conn


def test_extract_video_id_variants(tmp_path: Path):
    db_path = tmp_path / "yt.sqlite"

    def get_meta_connection():
        return _make_db_conn(db_path)

    service = YouTubeDurationService(
        get_meta_connection=get_meta_connection,
        get_reader=lambda: _ReaderCtx(_FakeReader([])),
        user_agent="LectioTest/1.0",
    )

    assert service.extract_video_id("https://youtu.be/ABCDEFGHIJK") == "ABCDEFGHIJK"
    assert service.extract_video_id("https://www.youtube.com/watch?v=ABCDEFGHIJK") == "ABCDEFGHIJK"
    assert service.extract_video_id("https://www.youtube.com/shorts/ABCDEFGHIJK") == "ABCDEFGHIJK"


def test_get_cached_duration_falls_back_to_db(tmp_path: Path):
    db_path = tmp_path / "yt.sqlite"

    def get_meta_connection():
        return _make_db_conn(db_path)

    with get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO youtube_video_duration(video_id, duration_seconds, duration_display, fetched_at) VALUES (?, ?, ?, datetime('now'))",
            ("ABCDEFGHIJK", 95, "1:35"),
        )

    service = YouTubeDurationService(
        get_meta_connection=get_meta_connection,
        get_reader=lambda: _ReaderCtx(_FakeReader([])),
        user_agent="LectioTest/1.0",
        cache={},
    )

    assert service.get_cached_duration("ABCDEFGHIJK") == (95, "1:35")
    assert service.cache["ABCDEFGHIJK"] == (95, "1:35")


def test_fetch_and_store_durations_for_feed_persists_missing_video(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "yt.sqlite"

    entries = [_FakeEntry("https://www.youtube.com/watch?v=ABCDEFGHIJK")]

    def get_meta_connection():
        return _make_db_conn(db_path)

    service = YouTubeDurationService(
        get_meta_connection=get_meta_connection,
        get_reader=lambda: _ReaderCtx(_FakeReader(entries)),
        user_agent="LectioTest/1.0",
    )

    monkeypatch.setattr(service, "get_video_duration", lambda _video_id: (360, "6:00"))

    service.fetch_and_store_durations_for_feed("https://www.youtube.com/feeds/videos.xml?channel_id=test")

    assert service.cache["ABCDEFGHIJK"] == (360, "6:00")
    with get_meta_connection() as conn:
        row = conn.execute(
            "SELECT duration_seconds, duration_display FROM youtube_video_duration WHERE video_id = ?",
            ("ABCDEFGHIJK",),
        ).fetchone()
    assert row is not None
    assert row["duration_seconds"] == 360
    assert row["duration_display"] == "6:00"
