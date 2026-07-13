from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

import pytest

import services.feed_refresh as _feed_refresh_mod
from services.feed_refresh import FeedRefreshService


@pytest.fixture(autouse=True)
def _no_real_pace_sleep(monkeypatch):
    """Neutralize the high-fanout pacing sleep so tests don't wait in real time.
    The pacing-specific tests re-patch sleep with their own recorder."""
    monkeypatch.setattr(_feed_refresh_mod.time, "sleep", lambda s: None)


class _ReaderCtx:
    def __init__(self, reader):
        self._reader = reader

    def __enter__(self):
        return self._reader

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeReader:
    def __init__(self, fail_urls: set[str] | None = None):
        self.fail_urls = fail_urls or set()
        self.updated: list[str] = []

    def update_feed(self, feed_url: str):
        self.updated.append(feed_url)
        if feed_url in self.fail_urls:
            raise RuntimeError("404 Not Found")


def _make_conn(db_path: Path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feed_failure_state (
            feed_url TEXT PRIMARY KEY,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            next_retry_at REAL,
            last_error TEXT,
            last_failure_at REAL,
            last_success_at REAL,
            acknowledged_at REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS domain_failure_state (
            domain TEXT PRIMARY KEY,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            next_retry_at REAL,
            last_failure_at REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS folder_feeds (
            folder_id INTEGER NOT NULL,
            feed_url TEXT NOT NULL,
            PRIMARY KEY(folder_id, feed_url)
        )
        """
    )
    return conn


def _build_service(db_path: Path, reader, yt_calls: list[str], lead_calls: list[str],
                   on_fetch_refused=None):
    def get_meta_connection():
        return _make_conn(db_path)

    return FeedRefreshService(
        get_meta_connection=get_meta_connection,
        get_reader=lambda: _ReaderCtx(reader),
        fetch_and_store_youtube_durations=lambda feed_url: yt_calls.append(feed_url),
        fetch_and_store_lead_images=lambda feed_url: lead_calls.append(feed_url),
        format_datetime_for_ui=lambda _dt: "formatted",
        logger=logging.getLogger("test-refresh"),
        refresh_debug_enabled=False,
        failed_feed_backoff_base_seconds=60,
        failed_feed_backoff_max_seconds=24 * 60 * 60,
        on_fetch_refused=on_fetch_refused,
    )


def test_is_fetch_refusal_classifies_only_refusals():
    f = FeedRefreshService._is_fetch_refusal
    assert f(RuntimeError("HTTP 415 Unsupported Media Type"))
    assert f(RuntimeError("403 Forbidden"))
    assert f(RuntimeError("read operation timed out"))
    assert f(RuntimeError("server returned 503"))
    assert not f(RuntimeError("404 Not Found"))
    assert not f(RuntimeError("410 Gone"))
    assert not f(RuntimeError("401 Unauthorized"))


class _RefusingReader:
    """Fails the first update of a feed with a refusal, succeeds on retry."""
    def __init__(self, refuse_urls: set[str]):
        self.refuse_urls = set(refuse_urls)
        self.attempts: list[str] = []

    def update_feed(self, feed_url: str):
        self.attempts.append(feed_url)
        if feed_url in self.refuse_urls:
            self.refuse_urls.discard(feed_url)  # refuse only the first attempt
            raise RuntimeError("HTTP 415 Unsupported Media Type")


def test_refusal_triggers_flag_and_retry(tmp_path: Path):
    db_path = tmp_path / "m.sqlite"
    reader = _RefusingReader({"https://blocked.test/feed"})
    flagged: list[str] = []

    def on_refused(url: str) -> bool:
        flagged.append(url)
        return True  # newly flagged → service should retry once

    service = _build_service(db_path, reader, [], [], on_fetch_refused=on_refused)
    service.update_feeds(["https://blocked.test/feed"])

    assert flagged == ["https://blocked.test/feed"]
    # Retried after flagging (two attempts total).
    assert reader.attempts == ["https://blocked.test/feed", "https://blocked.test/feed"]
    # The successful retry cleared the failure state.
    with _make_conn(db_path) as conn:
        row = conn.execute(
            "SELECT consecutive_failures, last_success_at FROM feed_failure_state WHERE feed_url = ?",
            ("https://blocked.test/feed",),
        ).fetchone()
    assert row is not None and row["consecutive_failures"] == 0
    assert row["last_success_at"] is not None


def test_refusal_no_retry_when_not_newly_flagged(tmp_path: Path):
    # Already-flagged feed (callback returns False) must not retry-loop.
    reader = _RefusingReader({"https://blocked.test/feed"})
    service = _build_service(tmp_path / "m2.sqlite", reader, [], [], on_fetch_refused=lambda _u: False)
    service.update_feeds(["https://blocked.test/feed"])
    assert reader.attempts == ["https://blocked.test/feed"]  # no retry


def test_compute_backoff_caps_at_max(tmp_path: Path):
    reader = _FakeReader()
    calls: list[str] = []
    lead_calls: list[str] = []
    service = _build_service(tmp_path / "meta.sqlite", reader, calls, lead_calls)

    assert service.compute_failed_feed_backoff_seconds(1) == 60
    assert service.compute_failed_feed_backoff_seconds(2) == 120
    assert service.compute_failed_feed_backoff_seconds(30) == 24 * 60 * 60


def test_update_feeds_records_success_and_failure(tmp_path: Path):
    db_path = tmp_path / "meta.sqlite"
    reader = _FakeReader(fail_urls={"https://example.com/fail.xml"})
    yt_calls: list[str] = []
    lead_calls: list[str] = []
    service = _build_service(db_path, reader, yt_calls, lead_calls)

    service.update_feeds(["https://example.com/good.xml", "https://example.com/fail.xml"])

    with _make_conn(db_path) as conn:
        ok_row = conn.execute(
            "SELECT consecutive_failures, next_retry_at, last_error FROM feed_failure_state WHERE feed_url = ?",
            ("https://example.com/good.xml",),
        ).fetchone()
        fail_row = conn.execute(
            "SELECT consecutive_failures, next_retry_at, last_error FROM feed_failure_state WHERE feed_url = ?",
            ("https://example.com/fail.xml",),
        ).fetchone()

    assert ok_row is not None
    assert ok_row["consecutive_failures"] == 0
    assert ok_row["next_retry_at"] is None
    assert ok_row["last_error"] is None

    assert fail_row is not None
    assert fail_row["consecutive_failures"] == 1
    assert fail_row["next_retry_at"] is not None
    assert "404" in fail_row["last_error"]

    assert reader.updated == ["https://example.com/good.xml", "https://example.com/fail.xml"]
    assert yt_calls == ["https://example.com/good.xml", "https://example.com/fail.xml"]
    assert lead_calls == ["https://example.com/good.xml", "https://example.com/fail.xml"]


def test_update_feeds_enhance_false_defers_enhancement(tmp_path: Path):
    db_path = tmp_path / "meta.sqlite"
    reader = _FakeReader()
    yt_calls: list[str] = []
    lead_calls: list[str] = []
    service = _build_service(db_path, reader, yt_calls, lead_calls)

    # enhance=False ingests entries but skips the network-heavy enhancement,
    service.update_feeds(["https://example.com/good.xml"], enhance=False)
    assert reader.updated == ["https://example.com/good.xml"]
    assert yt_calls == []
    assert lead_calls == []

    # which a caller can then run off the request path via enhance_feeds().
    service.enhance_feeds(["https://example.com/good.xml"])
    assert yt_calls == ["https://example.com/good.xml"]
    assert lead_calls == ["https://example.com/good.xml"]


def test_update_feeds_skips_when_backoff_not_elapsed(tmp_path: Path):
    db_path = tmp_path / "meta.sqlite"
    reader = _FakeReader()
    yt_calls: list[str] = []
    lead_calls: list[str] = []
    service = _build_service(db_path, reader, yt_calls, lead_calls)

    with _make_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO feed_failure_state(feed_url, consecutive_failures, next_retry_at, last_error) VALUES (?, ?, ?, ?)",
            ("https://example.com/skip.xml", 3, time.time() + 3600, "some error"),
        )

    service.update_feeds(["https://example.com/skip.xml"])

    assert reader.updated == []
    # Current behavior: youtube duration follow-up still runs for each requested feed URL.
    assert yt_calls == ["https://example.com/skip.xml"]
    assert lead_calls == ["https://example.com/skip.xml"]


def test_get_problematic_feeds_formats_retry_display(tmp_path: Path):
    db_path = tmp_path / "meta.sqlite"
    reader = _FakeReader()
    yt_calls: list[str] = []
    lead_calls: list[str] = []
    service = _build_service(db_path, reader, yt_calls, lead_calls)

    with _make_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO folder_feeds(folder_id, feed_url) VALUES (?, ?)",
            (1, "https://example.com/problem.xml"),
        )
        conn.execute(
            "INSERT INTO feed_failure_state(feed_url, consecutive_failures, next_retry_at, last_error) VALUES (?, ?, ?, ?)",
            ("https://example.com/problem.xml", 2, 1_900_000_000.0, "bad feed"),
        )
        rows = service.get_problematic_feeds(conn)

    assert len(rows) == 1
    row = rows[0]
    assert row["feed_url"] == "https://example.com/problem.xml"
    assert row["next_retry_display"] == "formatted"


class _NewFeedReader(_FakeReader):
    """get_feed reports last_updated=None (never fetched) for the given URLs."""
    def __init__(self, never_updated_urls: set[str], fail_urls: set[str] | None = None):
        super().__init__(fail_urls)
        self.never_updated_urls = set(never_updated_urls)

    def get_feed(self, feed_url: str, _default=None):
        class _F:
            update_after = None
            last_updated = None if feed_url in self.never_updated_urls else 1.0
        return _F()


def test_domain_backoff_skips_already_fetched_feed(tmp_path: Path):
    """A feed that has fetched before stays skipped while its domain is in backoff."""
    db_path = tmp_path / "meta.sqlite"
    reader = _NewFeedReader(never_updated_urls=set())
    service = _build_service(db_path, reader, [], [])

    with _make_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO domain_failure_state(domain, consecutive_failures, next_retry_at) VALUES (?, ?, ?)",
            ("example.com", 13, time.time() + 3600),
        )

    service.update_feeds(["https://example.com/other.xml"])
    assert reader.updated == []


def test_domain_backoff_does_not_block_first_fetch_of_new_feed(tmp_path: Path):
    """A just-subscribed feed (never fetched) gets its initial refresh even while
    the domain is in backoff earned by other feeds' failures — otherwise the new
    subscription sits empty and invisible for hours (pinboard t:tag 500s put
    feeds.pinboard.in in backoff, silently skipping a newly added feed)."""
    db_path = tmp_path / "meta.sqlite"
    new_url = "https://example.com/new.xml"
    reader = _NewFeedReader(never_updated_urls={new_url})
    service = _build_service(db_path, reader, [], [])

    with _make_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO domain_failure_state(domain, consecutive_failures, next_retry_at) VALUES (?, ?, ?)",
            ("example.com", 13, time.time() + 3600),
        )

    service.update_feeds([new_url])
    assert reader.updated == [new_url]


def test_feed_level_backoff_still_applies_to_new_feed(tmp_path: Path):
    """The new-feed exemption bypasses only the domain backoff; a feed-level
    backoff on the feed itself is still honored."""
    db_path = tmp_path / "meta.sqlite"
    new_url = "https://example.com/new.xml"
    reader = _NewFeedReader(never_updated_urls={new_url})
    service = _build_service(db_path, reader, [], [])

    with _make_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO feed_failure_state(feed_url, consecutive_failures, next_retry_at, last_error) VALUES (?, ?, ?, ?)",
            (new_url, 2, time.time() + 3600, "boom"),
        )

    service.update_feeds([new_url])
    assert reader.updated == []


class _NotFoundError(RuntimeError):
    """Mimics a reader update exception carrying HTTP status via http_info."""
    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        class _Info:
            pass
        self.http_info = _Info()
        self.http_info.status = status


class _StatusFailReader(_FakeReader):
    def __init__(self, fail_statuses: dict[str, int]):
        super().__init__()
        self.fail_statuses = fail_statuses

    def update_feed(self, feed_url: str):
        self.updated.append(feed_url)
        status = self.fail_statuses.get(feed_url)
        if status:
            raise _NotFoundError(status)

    def get_feed(self, feed_url: str, _default=None):
        class _F:
            update_after = None
            last_updated = 1.0
        return _F()


class _FailReader(_FakeReader):
    """Fails the given URLs. If a url maps to an int it raises with that HTTP
    status (via http_info); otherwise it raises a transport-style error with no
    http_info (mirroring reader, whose 404s often arrive with status=None)."""
    def __init__(self, fails):
        super().__init__()
        self.fails = dict(fails) if isinstance(fails, dict) else {u: None for u in fails}

    def update_feed(self, feed_url: str):
        self.updated.append(feed_url)
        if feed_url in self.fails:
            status = self.fails[feed_url]
            raise _NotFoundError(status) if status else RuntimeError("connection refused")

    def get_feed(self, feed_url: str, _default=None):
        class _F:
            update_after = None
            last_updated = 1.0
        return _F()


def _yt(n: int) -> str:
    return f"https://www.youtube.com/feeds/videos.xml?channel_id=C{n}"


def test_high_fanout_domain_never_backs_off(tmp_path: Path):
    """A host with many feeds in the batch (youtube.com) is exempt from
    domain-level backoff: a few dead channels must never create a lock that
    stalls the other subscriptions. Per-feed backoff still applies to the dead
    ones."""
    db_path = tmp_path / "meta.sqlite"
    feeds = [_yt(i) for i in range(10)]
    dead = {feeds[0]: 404, feeds[1]: 404, feeds[2]: 404}  # 3 dead channels
    reader = _FailReader(dead)
    service = _build_service(db_path, reader, [], [])

    service.update_feeds(feeds)
    assert set(reader.updated) == set(feeds)  # every feed attempted, none skipped
    with _make_conn(db_path) as conn:
        domain_row = conn.execute("SELECT * FROM domain_failure_state WHERE domain = 'www.youtube.com'").fetchone()
        feed_row = conn.execute("SELECT consecutive_failures FROM feed_failure_state WHERE feed_url = ?", (feeds[0],)).fetchone()
    assert domain_row is None          # high-fanout host never domain-backed-off
    assert feed_row and feed_row[0] == 1  # dead channel still earns per-feed backoff


def test_high_fanout_domain_ignores_stale_backoff(tmp_path: Path):
    """A pre-existing (stuck) domain backoff must not skip a high-fanout host's
    feeds — that was the original starvation."""
    db_path = tmp_path / "meta.sqlite"
    feeds = [_yt(i) for i in range(10)]
    reader = _FailReader({})  # all succeed
    service = _build_service(db_path, reader, [], [])
    with _make_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO domain_failure_state(domain, consecutive_failures, next_retry_at, last_failure_at) VALUES (?, ?, ?, ?)",
            ("www.youtube.com", 17, time.time() + 80000, time.time()),
        )

    service.update_feeds(feeds)
    assert set(reader.updated) == set(feeds)  # not skipped despite the stale lock


def test_low_fanout_single_failure_no_skip(tmp_path: Path):
    """One failure on a small host is tracked but doesn't activate the skip."""
    db_path = tmp_path / "meta.sqlite"
    url = "https://down.example/feed.xml"
    reader = _FailReader({url: None})  # transport-style
    service = _build_service(db_path, reader, [], [])

    service.update_feeds([url])
    with _make_conn(db_path) as conn:
        row = conn.execute(
            "SELECT consecutive_failures, next_retry_at FROM domain_failure_state WHERE domain = 'down.example'"
        ).fetchone()
    assert row and row[0] == 1 and row[1] is None  # tracked, no active backoff


def test_low_fanout_backs_off_after_threshold_capped(tmp_path: Path):
    """A small host that's genuinely down (>= MIN_FAILURES consecutive failures)
    backs off, capped so it re-probes within ~1h."""
    db_path = tmp_path / "meta.sqlite"
    urls = ["https://down.example/a.xml", "https://down.example/b.xml", "https://down.example/c.xml"]
    reader = _FailReader({u: None for u in urls})
    service = _build_service(db_path, reader, [], [])

    service.update_feeds(urls)  # 3 consecutive failures on a low-fanout host
    with _make_conn(db_path) as conn:
        row = conn.execute(
            "SELECT consecutive_failures, next_retry_at FROM domain_failure_state WHERE domain = 'down.example'"
        ).fetchone()
    assert row and row[0] >= 3
    assert row[1] is not None and row[1] <= time.time() + 3600 + 5  # active, capped ~1h


def test_high_fanout_requests_are_paced(tmp_path: Path, monkeypatch):
    """Requests to a high-fanout host are spaced out so a big burst isn't
    throttled — every request after the first on that host waits."""
    import services.feed_refresh as fr
    db_path = tmp_path / "meta.sqlite"
    feeds = [_yt(i) for i in range(10)]  # 10 feeds on one host
    reader = _FailReader({})  # all succeed instantly
    service = _build_service(db_path, reader, [], [])
    sleeps: list[float] = []
    monkeypatch.setattr(fr.time, "sleep", lambda s: sleeps.append(s))

    service.update_feeds(feeds)
    assert len([s for s in sleeps if s > 0]) >= len(feeds) - 1  # all but the first paced


def test_low_fanout_requests_are_not_paced(tmp_path: Path, monkeypatch):
    """Small hosts are not paced — no needless delay on ordinary feeds."""
    import services.feed_refresh as fr
    db_path = tmp_path / "meta.sqlite"
    feeds = ["https://a.example/f.xml", "https://b.example/f.xml"]
    reader = _FailReader({})
    service = _build_service(db_path, reader, [], [])
    sleeps: list[float] = []
    monkeypatch.setattr(fr.time, "sleep", lambda s: sleeps.append(s))

    service.update_feeds(feeds)
    assert not [s for s in sleeps if s > 0]
