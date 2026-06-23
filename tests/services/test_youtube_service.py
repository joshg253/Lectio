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
        get_durations_connection=get_meta_connection,
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
        get_durations_connection=get_meta_connection,
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
        get_durations_connection=get_meta_connection,
        get_reader=lambda: _ReaderCtx(_FakeReader(entries)),
        user_agent="LectioTest/1.0",
    )

    monkeypatch.setattr(service, "get_video_durations_batch",
                        lambda ids: {vid: (360, "6:00") for vid in ids})

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


def test_get_video_durations_batch_parses_multiple(tmp_path: Path, monkeypatch):
    import httpx

    db_path = tmp_path / "yt.sqlite"
    service = YouTubeDurationService(
        get_durations_connection=lambda: _make_db_conn(db_path),
        get_reader=lambda: _ReaderCtx(_FakeReader([])),
        user_agent="LectioTest/1.0",
        api_key_provider=lambda: "fake-key",
    )

    captured = {}

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"items": [
                {"id": "AAAAAAAAAAA", "contentDetails": {"duration": "PT1H2M3S"}},
                {"id": "BBBBBBBBBBB", "contentDetails": {"duration": "PT45S"}},
                # CCCCCCCCCCC intentionally omitted (private/deleted) → (None, None)
            ]}

    def _fake_get(url, params=None, timeout=None):
        captured["ids"] = params["id"]
        return _Resp()

    monkeypatch.setattr(httpx, "get", _fake_get)
    out = service.get_video_durations_batch(["AAAAAAAAAAA", "BBBBBBBBBBB", "CCCCCCCCCCC"])
    # One batched call carried all three ids (1 quota unit, not 3).
    assert captured["ids"] == "AAAAAAAAAAA,BBBBBBBBBBB,CCCCCCCCCCC"
    assert out["AAAAAAAAAAA"] == (3723, "1:02:03")
    assert out["BBBBBBBBBBB"] == (45, "0:45")
    assert "CCCCCCCCCCC" not in out  # absent → caller stores (None, None)


def test_stale_negative_is_retried_and_self_heals(tmp_path: Path, monkeypatch):
    # A transient API failure (or a live/upcoming stream) caches (None, None). It
    # must NOT blank the duration forever: once the cached negative goes stale, the
    # next refresh re-fetches and fills it in.
    db_path = tmp_path / "yt.sqlite"
    entries = [_FakeEntry("https://www.youtube.com/watch?v=ABCDEFGHIJK")]

    def get_meta_connection():
        return _make_db_conn(db_path)

    # Seed a STALE negative (fetched_at well beyond the retry window).
    with get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO youtube_video_duration(video_id, duration_seconds, duration_display, fetched_at)"
            " VALUES (?, NULL, NULL, datetime('now', '-2 days'))",
            ("ABCDEFGHIJK",),
        )

    service = YouTubeDurationService(
        get_durations_connection=get_meta_connection,
        get_reader=lambda: _ReaderCtx(_FakeReader(entries)),
        user_agent="LectioTest/1.0",
    )
    monkeypatch.setattr(service, "get_video_durations_batch", lambda ids: {v: (95, "1:35") for v in ids})
    service.fetch_and_store_durations_for_feed("https://www.youtube.com/feeds/videos.xml?channel_id=t")
    assert service.cache["ABCDEFGHIJK"] == (95, "1:35")


def test_fresh_negative_is_not_refetched(tmp_path: Path, monkeypatch):
    # A recent negative is respected (no API re-hit every refresh for genuinely
    # length-less videos).
    db_path = tmp_path / "yt.sqlite"
    entries = [_FakeEntry("https://www.youtube.com/watch?v=ABCDEFGHIJK")]

    def get_meta_connection():
        return _make_db_conn(db_path)

    with get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO youtube_video_duration(video_id, duration_seconds, duration_display, fetched_at)"
            " VALUES (?, NULL, NULL, datetime('now'))",
            ("ABCDEFGHIJK",),
        )

    service = YouTubeDurationService(
        get_durations_connection=get_meta_connection,
        get_reader=lambda: _ReaderCtx(_FakeReader(entries)),
        user_agent="LectioTest/1.0",
    )
    calls = []
    monkeypatch.setattr(service, "get_video_durations_batch",
                        lambda ids: (calls.extend(ids) or {v: (95, "1:35") for v in ids}))
    service.fetch_and_store_durations_for_feed("https://www.youtube.com/feeds/videos.xml?channel_id=t")
    assert calls == []  # fresh negative respected
    assert service.cache["ABCDEFGHIJK"] == (None, None)
