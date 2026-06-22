"""Per-feed browser-identity flag storage (browser_ua_feeds)."""
from __future__ import annotations

import pytest

import main
from services import tenancy


def _reset_pools():
    main._reader_thread_local.pool = None
    main._meta_conn_local.pool = None


@pytest.fixture
def meta(tmp_path):
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
        yield main.get_meta_connection()
    finally:
        _reset_pools()
        tenancy._layout = saved


def test_flag_and_get(meta):
    assert main.get_browser_ua_feed_urls(meta) == set()
    newly = main.flag_browser_ua_feed(meta, "https://blocked.test/feed", reason="test")
    assert newly is True
    assert main.get_browser_ua_feed_urls(meta) == {"https://blocked.test/feed"}


def test_flag_is_idempotent(meta):
    assert main.flag_browser_ua_feed(meta, "https://x.test/feed") is True
    assert main.flag_browser_ua_feed(meta, "https://x.test/feed") is False  # already flagged


def test_unflag(meta):
    main.flag_browser_ua_feed(meta, "https://x.test/feed")
    main.unflag_browser_ua_feed(meta, "https://x.test/feed")
    assert main.get_browser_ua_feed_urls(meta) == set()


def test_blank_url_not_flagged(meta):
    assert main.flag_browser_ua_feed(meta, "   ") is False
    assert main.get_browser_ua_feed_urls(meta) == set()
