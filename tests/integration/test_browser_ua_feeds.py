"""Per-feed browser-identity flag storage (browser_ua_feeds)."""

from __future__ import annotations

import pytest
from _tenancy_helpers import configure_test_tenancy

import main
from services import tenancy


def _reset_pools():
    main.close_thread_db_pools()


@pytest.fixture
def meta(tmp_path):
    saved = tenancy._layout
    _reset_pools()
    configure_test_tenancy(tmp_path)
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


@pytest.mark.parametrize(
    ("flag", "table"),
    [
        ("flag_browser_ua_feed", "browser_ua_feeds"),
        ("flag_proxy_feed", "proxy_feeds"),
        ("flag_tailscale_feed", "tailscale_feeds"),
        ("flag_flaresolverr_feed", "flaresolverr_feeds"),
    ],
)
def test_reasserting_a_flag_refreshes_flagged_at_and_manual_takes_over_the_reason(meta, flag, table):
    """The escalation re-probe compares flagged_at and reads reason, so a re-flag must move one and a manual flag must set the other."""
    url = "https://x.test/feed"
    fn = getattr(main, flag)
    assert fn(meta, url, reason="refused") is True
    meta.execute(f"UPDATE {table} SET flagged_at = '2026-01-01 00:00:00' WHERE feed_url = ?", (url,))
    assert fn(meta, url, reason="still refused") is False  # not newly flagged, but re-asserted
    flagged_at, reason = meta.execute(f"SELECT flagged_at, reason FROM {table} WHERE feed_url = ?", (url,)).fetchone()
    assert flagged_at > "2026-01-01 00:00:00"
    assert reason == "refused"  # an automatic re-flag keeps the original reason
    assert fn(meta, url, reason="manual") is False
    assert meta.execute(f"SELECT reason FROM {table} WHERE feed_url = ?", (url,)).fetchone()[0] == "manual"
