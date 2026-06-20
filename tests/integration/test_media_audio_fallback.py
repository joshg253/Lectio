"""media:content podcast-audio fallback. reader drops media:content, so the app
re-parses the raw feed in the background and caches per-entry audio URLs in
entry_media_audio; _resolve_entry_audio_url consults that cache (and only
re-scans a feed when its TTL is due)."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

import main
from services import tenancy

FEED = "https://pod.test/feed"


def _reset_pools():
    main._reader_thread_local.pool = None
    main._meta_conn_local.pool = None


@pytest.fixture
def configured(tmp_path):
    saved = tenancy._layout
    _reset_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    try:
        yield
    finally:
        _reset_pools()
        tenancy._layout = saved


def _entry(enclosures=None, link=""):
    return SimpleNamespace(enclosures=enclosures or [], link=link)


def test_cached_media_audio_is_returned(configured):
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO entry_media_audio (feed_url, entry_id, audio_url) VALUES (?, ?, ?)",
            (FEED, "e1", "https://cdn.test/e1.mp3"),
        )
        conn.commit()
        url = main._resolve_entry_audio_url(conn, FEED, "e1", _entry())
    assert url == "https://cdn.test/e1.mp3"


def test_standard_enclosure_takes_precedence(configured):
    enc = SimpleNamespace(href="https://cdn.test/direct.mp3", type="audio/mpeg")
    with main.get_meta_connection() as conn:
        url = main._resolve_entry_audio_url(conn, FEED, "e1", _entry(enclosures=[enc]))
    assert url == "https://cdn.test/direct.mp3"


def test_cache_miss_enqueues_scan_once(configured, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_queue_media_audio_scan", lambda fu: calls.append(fu))
    with main.get_meta_connection() as conn:
        assert main._resolve_entry_audio_url(conn, FEED, "e1", _entry()) is None
    assert calls == [FEED]


def test_recent_scan_with_no_audio_is_not_rescanned(configured, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_queue_media_audio_scan", lambda fu: calls.append(fu))
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO feed_media_scan (feed_url, scanned_at, found) VALUES (?, ?, 0)",
            (FEED, time.time()),
        )
        conn.commit()
        assert main._resolve_entry_audio_url(conn, FEED, "e1", _entry()) is None
    assert calls == []  # within the empty-feed TTL → no rescan


def test_stale_scan_is_rescanned(configured, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_queue_media_audio_scan", lambda fu: calls.append(fu))
    with main.get_meta_connection() as conn:
        old = time.time() - main._MEDIA_SCAN_TTL_EMPTY - 10
        conn.execute(
            "INSERT INTO feed_media_scan (feed_url, scanned_at, found) VALUES (?, ?, 0)",
            (FEED, old),
        )
        conn.commit()
        main._resolve_entry_audio_url(conn, FEED, "e1", _entry())
    assert calls == [FEED]


def test_suggested_audio_feed_stored_when_no_media(configured, monkeypatch):
    # Feed itself has no media:content audio, so the scan should fall back to
    # discovering a podcast-host feed and record the suggestion.
    class _Resp:
        status_code = 200
        content = (b'<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>'
                   b"<item><guid>e1</guid></item></channel></rss>")

    monkeypatch.setattr(main.url_guard, "safe_get", lambda client, url, **kw: _Resp())
    monkeypatch.setattr(main, "_discover_suggested_audio_feed",
                        lambda fu: "https://feeds.libsyn.com/21070/rss")
    main._scan_feed_media_audio(FEED)
    with main.get_meta_connection() as conn:
        assert main._get_suggested_audio_feed(conn, FEED) == "https://feeds.libsyn.com/21070/rss"


def test_discovery_fetches_with_browser_user_agent(configured, monkeypatch):
    # Buzzsprout/Libsyn host feeds and many episode pages sit behind Cloudflare,
    # which 403s the terse default UA. Discovery must use a browser UA so the
    # episode-page fetch isn't silently blocked.
    import contextlib

    captured = {}

    class _FakeClient:
        def __init__(self, *a, headers=None, **kw):
            captured["ua"] = (headers or {}).get("User-Agent")
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    @contextlib.contextmanager
    def _fake_reader():
        yield SimpleNamespace(
            get_entries=lambda **kw: [SimpleNamespace(link="https://ep.test/1")]
        )

    class _Resp:
        status_code = 200
        text = "<html>https://feeds.buzzsprout.com/1501960.rss</html>"

    monkeypatch.setattr(main, "get_reader", _fake_reader)
    monkeypatch.setattr(main.httpx, "Client", _FakeClient)
    monkeypatch.setattr(main.url_guard, "is_safe_outbound_url", lambda url: True)
    monkeypatch.setattr(main.url_guard, "safe_get", lambda client, url, **kw: _Resp())

    found = main._discover_suggested_audio_feed(FEED)
    assert found == "https://feeds.buzzsprout.com/1501960.rss"
    assert captured["ua"] == main.PODCAST_FETCH_USER_AGENT
    assert "Mozilla/" in captured["ua"]


def test_is_feed_subscribed(configured):
    with main.get_meta_connection() as conn:
        assert main._is_feed_subscribed(conn, "https://feeds.libsyn.com/21070/rss") is False
        conn.execute(
            "INSERT INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)",
            (1, "https://feeds.libsyn.com/21070/rss"),
        )
        conn.commit()
        assert main._is_feed_subscribed(conn, "https://feeds.libsyn.com/21070/rss") is True
        # slash-insensitive
        assert main._is_feed_subscribed(conn, "https://feeds.libsyn.com/21070/rss/") is True


def test_scan_persists_results_and_marker(configured, monkeypatch):
    feed_bytes = (
        '<?xml version="1.0"?>'
        '<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">'
        "<channel><title>T</title>"
        '<item><guid>e1</guid><media:content url="https://cdn.test/e1.mp3" type="audio/mpeg"/></item>'
        "</channel></rss>"
    )

    class _Resp:
        status_code = 200
        content = feed_bytes.encode()

    monkeypatch.setattr(main.url_guard, "safe_get", lambda client, url, **kw: _Resp())
    main._scan_feed_media_audio(FEED)
    with main.get_meta_connection() as conn:
        assert main._lookup_media_audio(conn, FEED, "e1") == "https://cdn.test/e1.mp3"
        row = conn.execute("SELECT found FROM feed_media_scan WHERE feed_url = ?", (FEED,)).fetchone()
        assert row[0] == 1
