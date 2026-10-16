from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from services.youtube import YouTubeDurationService


def _now_utc():
    return datetime.now(timezone.utc)


class _ReaderCtx:
    def __init__(self, reader):
        self._reader = reader

    def __enter__(self):
        return self._reader

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeEntry:
    def __init__(self, link: str, added=None):
        self.link = link
        self.added = added


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

    monkeypatch.setattr(service, "get_video_durations_batch", lambda ids: {vid: (360, "6:00", None, None) for vid in ids})

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

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "items": [
                    {"id": "AAAAAAAAAAA", "contentDetails": {"duration": "PT1H2M3S"}},
                    {"id": "BBBBBBBBBBB", "contentDetails": {"duration": "PT45S"}},
                    # CCCCCCCCCCC intentionally omitted (private/deleted) → (None, None)
                ]
            }

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

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "items": [
                    {
                        "id": "UPCOMINGVID",
                        "snippet": {"liveBroadcastContent": "upcoming"},
                        "contentDetails": {},
                        "liveStreamingDetails": {"scheduledStartTime": "2026-09-20T18:00:00Z"},
                    },
                ]
            }

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
        service,
        "get_video_durations_batch",
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
    monkeypatch.setattr(service, "get_video_durations_batch", lambda ids: calls.extend(ids) or {v: (95, "1:35") for v in ids})
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
        service,
        "get_video_durations_batch",
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


def test_failed_chunk_is_not_cached_as_a_negative(tmp_path: Path, monkeypatch):
    """A timeout/quota failure on one 50-id chunk used to be stored as a 6-hour "no duration" row for every id in it, so
    several-hour-old videos stayed length-less. Failed ids must stay uncached (retried next refresh); an id a successful
    chunk simply doesn't return is still a real negative."""
    import httpx

    db_path = tmp_path / "yt.sqlite"
    ids = [f"{i:011d}" for i in range(51)]  # 50 + 1 -> two chunks
    entries = [_FakeEntry(f"https://www.youtube.com/watch?v={v}") for v in ids]

    def get_meta_connection():
        return _make_db_conn(db_path)

    calls = {"n": 0}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            # First chunk: answers every id except the last (private/deleted).
            return {"items": [{"id": v, "contentDetails": {"duration": "PT1M"}} for v in ids[:49]]}

    def _fake_get(url, params=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Resp()
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(httpx, "get", _fake_get)
    service = YouTubeDurationService(
        get_durations_connection=get_meta_connection,
        get_reader=lambda: _ReaderCtx(_FakeReader(entries)),
        user_agent="LectioTest/1.0",
        api_key_provider=lambda: "fake-key",
    )
    service.fetch_and_store_durations_for_feed("https://www.youtube.com/feeds/videos.xml?channel_id=t")

    with get_meta_connection() as conn:
        stored = {r[0]: r[1] for r in conn.execute("SELECT video_id, duration_seconds FROM youtube_video_duration")}
    assert stored[ids[0]] == 60
    assert stored[ids[49]] is None  # in the answered chunk, not returned: a real negative
    assert ids[50] not in stored  # in the failed chunk: left for the next refresh


def _service_with_negative(tmp_path: Path, monkeypatch, *, entry, fetched_minutes_ago: int):
    """A service whose one video has a no-duration row fetched N minutes ago; returns (service, ids the batch was asked for)."""
    db_path = tmp_path / "yt.sqlite"

    def get_meta_connection():
        return _make_db_conn(db_path)

    with get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO youtube_video_duration(video_id, duration_seconds, duration_display, fetched_at)"
            " VALUES (?, NULL, NULL, datetime('now', ?))",
            ("ABCDEFGHIJK", f"-{fetched_minutes_ago} minutes"),
        )
    service = YouTubeDurationService(
        get_durations_connection=get_meta_connection,
        get_reader=lambda: _ReaderCtx(_FakeReader([entry])),
        user_agent="LectioTest/1.0",
    )
    asked: list[str] = []
    monkeypatch.setattr(service, "get_video_durations_batch", lambda ids: asked.extend(ids) or {})
    return service, asked


def test_young_entry_negative_retries_after_thirty_minutes(tmp_path: Path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    entry = _FakeEntry("https://www.youtube.com/watch?v=ABCDEFGHIJK")
    entry.added = datetime.now(timezone.utc) - timedelta(hours=2)
    service, asked = _service_with_negative(tmp_path, monkeypatch, entry=entry, fetched_minutes_ago=45)
    service.fetch_and_store_durations_for_feed("https://www.youtube.com/feeds/videos.xml?channel_id=t")
    assert asked == ["ABCDEFGHIJK"]


def test_old_entry_negative_waits_the_long_window(tmp_path: Path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    entry = _FakeEntry("https://www.youtube.com/watch?v=ABCDEFGHIJK")
    entry.added = datetime.now(timezone.utc) - timedelta(days=3)
    service, asked = _service_with_negative(tmp_path, monkeypatch, entry=entry, fetched_minutes_ago=45)
    service.fetch_and_store_durations_for_feed("https://www.youtube.com/feeds/videos.xml?channel_id=t")
    assert asked == []  # a genuinely length-less old video isn't re-polled every half hour


def test_failed_ids_back_off_between_refreshes(tmp_path: Path, monkeypatch):
    from datetime import datetime, timezone

    entry = _FakeEntry("https://www.youtube.com/watch?v=ABCDEFGHIJK")
    entry.added = datetime.now(timezone.utc)
    db_path = tmp_path / "yt.sqlite"
    service = YouTubeDurationService(
        get_durations_connection=lambda: _make_db_conn(db_path),
        get_reader=lambda: _ReaderCtx(_FakeReader([entry])),
        user_agent="LectioTest/1.0",
    )
    calls: list[list[str]] = []

    def _batch(ids):
        calls.append(list(ids))
        out = _youtube_service._BatchResult()
        out["OTHERVIDEO1"] = (60, "1:00", None, None)  # non-empty, so the all-failed early return doesn't hide the id
        out.failed.update(ids)
        return out

    from services import youtube as _youtube_service

    monkeypatch.setattr(service, "get_video_durations_batch", _batch)
    feed = "https://www.youtube.com/feeds/videos.xml?channel_id=t"
    service.fetch_and_store_durations_for_feed(feed)
    service.fetch_and_store_durations_for_feed(feed)
    assert calls == [["ABCDEFGHIJK"]]  # the second refresh skipped it


def test_refresh_upcoming_keeps_row_when_its_chunk_failed(tmp_path: Path, monkeypatch):
    from services import youtube as _youtube_service

    db_path = tmp_path / "yt.sqlite"

    def get_meta_connection():
        return _make_db_conn(db_path)

    with get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO youtube_video_duration"
            "(video_id, duration_seconds, duration_display, live_broadcast_content, scheduled_start_time, fetched_at)"
            " VALUES ('UPCOMING004', NULL, NULL, 'upcoming', '2026-09-20T18:00:00Z', datetime('now'))"
        )
    service = YouTubeDurationService(
        get_durations_connection=get_meta_connection,
        get_reader=lambda: _ReaderCtx(_FakeReader([])),
        user_agent="LectioTest/1.0",
        api_key_provider=lambda: "fake-key",
    )

    def _batch(ids):
        out = _youtube_service._BatchResult()
        out["SOMETHINGELS"] = (60, "1:00", None, None)
        out.failed.update(ids)
        return out

    monkeypatch.setattr(service, "get_video_durations_batch", _batch)
    service.refresh_upcoming_videos()
    with get_meta_connection() as conn:
        row = conn.execute("SELECT live_broadcast_content, scheduled_start_time FROM youtube_video_duration").fetchone()
    assert (row[0], row[1]) == ("upcoming", "2026-09-20T18:00:00Z")


def test_members_only_placeholder_row_does_not_block_the_duration_fetch(tmp_path: Path, monkeypatch):
    """A members-only check that runs before the first duration fetch creates the row; the next refresh must still fetch the duration."""
    db_path = tmp_path / "yt.sqlite"
    monkeypatch.setattr("services.youtube.httpx.get", lambda *a, **kw: _FakeResponse("<html>a normal video page</html>"))
    service = YouTubeDurationService(
        get_durations_connection=lambda: _make_db_conn(db_path),
        get_reader=lambda: _ReaderCtx(_FakeReader([_FakeEntry("https://www.youtube.com/watch?v=ABCDEFGHIJK", added=_now_utc())])),
        user_agent="LectioTest/1.0",
    )
    service.fetch_and_cache_members_only("ABCDEFGHIJK")
    requested: list[list[str]] = []

    def fake_batch(ids):
        requested.append(list(ids))
        return {"ABCDEFGHIJK": (125, "2:05", "none", None)}

    monkeypatch.setattr(service, "get_video_durations_batch", fake_batch)
    service.fetch_and_store_durations_for_feed("https://www.youtube.com/feeds/videos.xml?channel_id=UCx")
    assert requested == [["ABCDEFGHIJK"]]
    row = service._get_duration_row("ABCDEFGHIJK")
    assert row is not None and row[0] == 125


def _placeholder_then_entries(tmp_path: Path, monkeypatch, handler):
    """A service whose video has only the members-only placeholder row, with videos.list answered by *handler*."""
    import httpx

    db_path = tmp_path / "yt.sqlite"
    monkeypatch.setattr("services.youtube.httpx.get", handler)
    entry = _FakeEntry("https://www.youtube.com/watch?v=ABCDEFGHIJK", added=_now_utc())
    service = YouTubeDurationService(
        get_durations_connection=lambda: _make_db_conn(db_path),
        get_reader=lambda: _ReaderCtx(_FakeReader([entry])),
        user_agent="LectioTest/1.0",
        api_key_provider=lambda: "fake-key",
    )
    with service._get_durations_connection() as conn:
        conn.execute(
            "INSERT INTO youtube_video_duration(video_id, members_only, fetched_at) VALUES ('ABCDEFGHIJK', 0, '1970-01-01 00:00:00')"
        )
    return service, httpx


def test_a_failed_batch_backs_off_instead_of_retrying_every_refresh(tmp_path: Path, monkeypatch):
    calls: list[int] = []

    def boom(*a, **kw):
        calls.append(1)
        raise RuntimeError("timeout")

    service, _ = _placeholder_then_entries(tmp_path, monkeypatch, boom)
    feed = "https://www.youtube.com/feeds/videos.xml?channel_id=UCx"
    service.fetch_and_store_durations_for_feed(feed)
    service.fetch_and_store_durations_for_feed(feed)
    assert len(calls) == 1


def test_a_successful_answer_with_no_item_is_cached_as_a_negative(tmp_path: Path, monkeypatch):
    calls: list[int] = []

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"items": []}

    def empty(*a, **kw):
        calls.append(1)
        return _Resp()

    service, _ = _placeholder_then_entries(tmp_path, monkeypatch, empty)
    feed = "https://www.youtube.com/feeds/videos.xml?channel_id=UCx"
    service.fetch_and_store_durations_for_feed(feed)
    service.fetch_and_store_durations_for_feed(feed)
    assert len(calls) == 1  # the second refresh is inside the negative-retry window
