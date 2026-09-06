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
            live_broadcast_content TEXT,
            scheduled_start_time TEXT,
            members_only INTEGER,
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
            "INSERT INTO youtube_video_duration(video_id, duration_seconds, duration_display, fetched_at) "
            "VALUES (?, ?, ?, datetime('now'))",
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
                        lambda ids: {vid: (360, "6:00", None, None) for vid in ids})

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
        assert params is not None
        captured["ids"] = params["id"]
        return _Resp()

    monkeypatch.setattr(httpx, "get", _fake_get)
    out = service.get_video_durations_batch(["AAAAAAAAAAA", "BBBBBBBBBBB", "CCCCCCCCCCC"])
    # One batched call carried all three ids (1 quota unit, not 3).
    assert captured["ids"] == "AAAAAAAAAAA,BBBBBBBBBBB,CCCCCCCCCCC"
    assert out["AAAAAAAAAAA"] == (3723, "1:02:03", None, None)
    assert out["BBBBBBBBBBB"] == (45, "0:45", None, None)
    assert "CCCCCCCCCCC" not in out  # absent → caller stores (None, None, None, None)


def test_get_video_durations_batch_parses_upcoming_premiere(tmp_path: Path, monkeypatch):
    import httpx

    db_path = tmp_path / "yt.sqlite"
    service = YouTubeDurationService(
        get_durations_connection=lambda: _make_db_conn(db_path),
        get_reader=lambda: _ReaderCtx(_FakeReader([])),
        user_agent="LectioTest/1.0",
        api_key_provider=lambda: "fake-key",
    )

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"items": [
                {
                    "id": "UPCOMINGVID",
                    "snippet": {"liveBroadcastContent": "upcoming"},
                    "contentDetails": {},
                    "liveStreamingDetails": {"scheduledStartTime": "2026-09-20T18:00:00Z"},
                },
            ]}

    monkeypatch.setattr(httpx, "get", lambda url, params=None, timeout=None: _Resp())
    out = service.get_video_durations_batch(["UPCOMINGVID"])
    assert out["UPCOMINGVID"] == (None, None, "upcoming", "2026-09-20T18:00:00Z")


def test_upcoming_premiere_status_is_cached_and_retrievable(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "yt.sqlite"
    entries = [_FakeEntry("https://www.youtube.com/watch?v=UPCOMINGVID")]

    def get_meta_connection():
        return _make_db_conn(db_path)

    service = YouTubeDurationService(
        get_durations_connection=get_meta_connection,
        get_reader=lambda: _ReaderCtx(_FakeReader(entries)),
        user_agent="LectioTest/1.0",
    )
    monkeypatch.setattr(
        service, "get_video_durations_batch",
        lambda ids: {v: (None, None, "upcoming", "2026-09-20T18:00:00Z") for v in ids},
    )
    service.fetch_and_store_durations_for_feed("https://www.youtube.com/feeds/videos.xml?channel_id=t")

    assert service.get_cached_live_status("UPCOMINGVID") == ("upcoming", "2026-09-20T18:00:00Z")

    # A fresh instance (simulating the next process/refresh) must recover the
    # same status from the DB, not just from the warm in-memory cache.
    service2 = YouTubeDurationService(
        get_durations_connection=get_meta_connection,
        get_reader=lambda: _ReaderCtx(_FakeReader(entries)),
        user_agent="LectioTest/1.0",
    )
    assert service2.get_cached_live_status("UPCOMINGVID") == ("upcoming", "2026-09-20T18:00:00Z")


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
    monkeypatch.setattr(service, "get_video_durations_batch", lambda ids: {v: (95, "1:35", None, None) for v in ids})
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


def test_no_api_key_does_not_blank_an_upcoming_videos_live_status(tmp_path: Path, monkeypatch):
    # An "upcoming" video's duration is always None by design, so — unlike a
    # genuinely-unfetched video — it never ages out of to_fetch. A background
    # user with no API key must not be able to blank its live status on every
    # feed refresh that touches it.
    db_path = tmp_path / "yt.sqlite"
    entries = [_FakeEntry("https://www.youtube.com/watch?v=UPCOMING001")]

    def get_meta_connection():
        return _make_db_conn(db_path)

    with get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO youtube_video_duration"
            "(video_id, duration_seconds, duration_display, live_broadcast_content, scheduled_start_time, fetched_at)"
            " VALUES (?, NULL, NULL, 'upcoming', '2026-09-20T18:00:00Z', datetime('now', '-1 day'))",
            ("UPCOMING001",),
        )

    service = YouTubeDurationService(
        get_durations_connection=get_meta_connection,
        get_reader=lambda: _ReaderCtx(_FakeReader(entries)),
        user_agent="LectioTest/1.0",
        # No api_key_provider and no env var → get_video_durations_batch
        # returns {} for this "user".
    )
    service.fetch_and_store_durations_for_feed("https://www.youtube.com/feeds/videos.xml?channel_id=t")

    with get_meta_connection() as conn:
        row = conn.execute(
            "SELECT live_broadcast_content, scheduled_start_time FROM youtube_video_duration WHERE video_id = ?",
            ("UPCOMING001",),
        ).fetchone()
    assert row["live_broadcast_content"] == "upcoming"
    assert row["scheduled_start_time"] == "2026-09-20T18:00:00Z"


def test_refresh_upcoming_videos_updates_only_upcoming_rows(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "yt.sqlite"

    def get_meta_connection():
        return _make_db_conn(db_path)

    with get_meta_connection() as conn:
        conn.executemany(
            "INSERT INTO youtube_video_duration"
            "(video_id, duration_seconds, duration_display, live_broadcast_content, scheduled_start_time, fetched_at)"
            " VALUES (?, ?, ?, ?, ?, datetime('now'))",
            [
                ("UPCOMING002", None, None, "upcoming", "2026-09-20T18:00:00Z"),
                ("AIREDVIDEO01", 600, "10:00", None, None),
            ],
        )

    service = YouTubeDurationService(
        get_durations_connection=get_meta_connection,
        get_reader=lambda: _ReaderCtx(_FakeReader([])),
        user_agent="LectioTest/1.0",
        api_key_provider=lambda: "fake-key",
    )
    monkeypatch.setattr(
        service, "get_video_durations_batch",
        lambda ids: {v: (720, "12:00", "live", None) for v in ids},
    )

    checked = service.refresh_upcoming_videos()

    assert checked == 1  # only the upcoming row was queried/updated
    assert service.get_cached_live_status("UPCOMING002") == ("live", None)
    assert service.get_cached_duration("UPCOMING002") == (720, "12:00")
    # The already-aired video was never touched.
    assert service.get_cached_duration("AIREDVIDEO01") == (600, "10:00")


def test_refresh_upcoming_videos_no_key_does_not_blank_rows(tmp_path: Path):
    db_path = tmp_path / "yt.sqlite"

    def get_meta_connection():
        return _make_db_conn(db_path)

    with get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO youtube_video_duration"
            "(video_id, duration_seconds, duration_display, live_broadcast_content, scheduled_start_time, fetched_at)"
            " VALUES (?, NULL, NULL, 'upcoming', '2026-09-20T18:00:00Z', datetime('now'))",
            ("UPCOMING003",),
        )

    service = YouTubeDurationService(
        get_durations_connection=get_meta_connection,
        get_reader=lambda: _ReaderCtx(_FakeReader([])),
        user_agent="LectioTest/1.0",
        # No key → get_video_durations_batch returns {}.
    )
    checked = service.refresh_upcoming_videos()

    assert checked == 0
    with get_meta_connection() as conn:
        row = conn.execute(
            "SELECT live_broadcast_content, scheduled_start_time FROM youtube_video_duration WHERE video_id = ?",
            ("UPCOMING003",),
        ).fetchone()
    assert row["live_broadcast_content"] == "upcoming"
    assert row["scheduled_start_time"] == "2026-09-20T18:00:00Z"


def test_refresh_upcoming_videos_no_rows_is_a_noop(tmp_path: Path):
    db_path = tmp_path / "yt.sqlite"
    service = YouTubeDurationService(
        get_durations_connection=lambda: _make_db_conn(db_path),
        get_reader=lambda: _ReaderCtx(_FakeReader([])),
        user_agent="LectioTest/1.0",
        api_key_provider=lambda: "fake-key",
    )
    assert service.refresh_upcoming_videos() == 0


class _FakeResponse:
    """No failing status is ever exercised via this fake -- fetch failures are
    tested via a raising httpx.get instead (see the fetch-failure test below),
    matching how a real connection error surfaces, not a 4xx/5xx response."""
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self):
        pass


def test_get_cached_members_only_is_none_when_never_checked(tmp_path: Path):
    service = YouTubeDurationService(
        get_durations_connection=lambda: _make_db_conn(tmp_path / "yt.sqlite"),
        get_reader=lambda: _ReaderCtx(_FakeReader([])),
        user_agent="LectioTest/1.0",
    )
    assert service.get_cached_members_only("ABCDEFGHIJK") is None


def test_get_cached_members_only_falls_back_to_db(tmp_path: Path):
    db_path = tmp_path / "yt.sqlite"

    def get_meta_connection():
        return _make_db_conn(db_path)

    with get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO youtube_video_duration(video_id, members_only, fetched_at) VALUES (?, 1, datetime('now'))",
            ("ABCDEFGHIJK",),
        )

    service = YouTubeDurationService(
        get_durations_connection=get_meta_connection,
        get_reader=lambda: _ReaderCtx(_FakeReader([])),
        user_agent="LectioTest/1.0",
    )
    assert service.get_cached_members_only("ABCDEFGHIJK") is True


def test_fetch_and_cache_members_only_detects_the_real_badge(tmp_path: Path, monkeypatch):
    """The exact badge style YouTube's own watch-page JSON uses for a "Members
    only" video, confirmed live against a real members-only video."""
    db_path = tmp_path / "yt.sqlite"
    html = (
        '{"badges":[{"metadataBadgeRenderer":{"icon":{"iconType":"SPONSORSHIP_STAR"},'
        '"style":"BADGE_STYLE_TYPE_MEMBERS_ONLY","label":"Members only"}}]}'
    )
    monkeypatch.setattr("services.youtube.httpx.get", lambda *a, **kw: _FakeResponse(html))

    service = YouTubeDurationService(
        get_durations_connection=lambda: _make_db_conn(db_path),
        get_reader=lambda: _ReaderCtx(_FakeReader([])),
        user_agent="LectioTest/1.0",
    )
    assert service.fetch_and_cache_members_only("ABCDEFGHIJK") is True
    assert service.get_cached_members_only("ABCDEFGHIJK") is True
    with service._get_durations_connection() as conn:
        row = conn.execute(
            "SELECT members_only FROM youtube_video_duration WHERE video_id = ?",
            ("ABCDEFGHIJK",),
        ).fetchone()
    assert row["members_only"] == 1


def test_fetch_and_cache_members_only_false_for_a_normal_video(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "yt.sqlite"
    monkeypatch.setattr("services.youtube.httpx.get", lambda *a, **kw: _FakeResponse("<html>a normal video page</html>"))

    service = YouTubeDurationService(
        get_durations_connection=lambda: _make_db_conn(db_path),
        get_reader=lambda: _ReaderCtx(_FakeReader([])),
        user_agent="LectioTest/1.0",
    )
    assert service.fetch_and_cache_members_only("ABCDEFGHIJK") is False
    assert service.get_cached_members_only("ABCDEFGHIJK") is False


def test_fetch_and_cache_members_only_leaves_no_trace_on_fetch_failure(tmp_path: Path, monkeypatch):
    """A transient network error must not cache a wrong answer -- the next
    caller (the next refresh's hide-members-only pass) gets to retry, since
    there is no separate negative-retry timer for this cache."""
    db_path = tmp_path / "yt.sqlite"

    def _boom(*a, **kw):
        raise __import__("httpx").ConnectError("boom")

    monkeypatch.setattr("services.youtube.httpx.get", _boom)

    service = YouTubeDurationService(
        get_durations_connection=lambda: _make_db_conn(db_path),
        get_reader=lambda: _ReaderCtx(_FakeReader([])),
        user_agent="LectioTest/1.0",
    )
    assert service.fetch_and_cache_members_only("ABCDEFGHIJK") is None
    assert service.get_cached_members_only("ABCDEFGHIJK") is None
    with service._get_durations_connection() as conn:
        row = conn.execute(
            "SELECT * FROM youtube_video_duration WHERE video_id = ?",
            ("ABCDEFGHIJK",),
        ).fetchone()
    assert row is None


def test_fetch_and_cache_members_only_preserves_existing_duration_row(tmp_path: Path, monkeypatch):
    """A video's duration is usually cached first; checking members-only later
    must not clobber duration_seconds/duration_display on the same row."""
    db_path = tmp_path / "yt.sqlite"

    def get_meta_connection():
        return _make_db_conn(db_path)

    with get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO youtube_video_duration(video_id, duration_seconds, duration_display, fetched_at)"
            " VALUES (?, 360, '6:00', datetime('now'))",
            ("ABCDEFGHIJK",),
        )
    html = '{"style":"BADGE_STYLE_TYPE_MEMBERS_ONLY"}'
    monkeypatch.setattr("services.youtube.httpx.get", lambda *a, **kw: _FakeResponse(html))

    service = YouTubeDurationService(
        get_durations_connection=get_meta_connection,
        get_reader=lambda: _ReaderCtx(_FakeReader([])),
        user_agent="LectioTest/1.0",
    )
    assert service.fetch_and_cache_members_only("ABCDEFGHIJK") is True
    with get_meta_connection() as conn:
        row = conn.execute(
            "SELECT duration_seconds, duration_display, members_only FROM youtube_video_duration WHERE video_id = ?",
            ("ABCDEFGHIJK",),
        ).fetchone()
    assert row["duration_seconds"] == 360
    assert row["duration_display"] == "6:00"
    assert row["members_only"] == 1
