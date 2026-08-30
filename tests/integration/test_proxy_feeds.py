"""Per-feed as-needed-proxy flag storage (proxy_feeds)."""
from __future__ import annotations

import pytest

import main
from services import tenancy


def _reset_pools():
    main.close_thread_db_pools()


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
    assert main.get_proxy_feed_urls(meta) == set()
    newly = main.flag_proxy_feed(meta, "https://blocked.test/feed", reason="test")
    assert newly is True
    assert main.get_proxy_feed_urls(meta) == {"https://blocked.test/feed"}


def test_flag_is_idempotent(meta):
    assert main.flag_proxy_feed(meta, "https://x.test/feed") is True
    assert main.flag_proxy_feed(meta, "https://x.test/feed") is False  # already flagged


def test_unflag(meta):
    main.flag_proxy_feed(meta, "https://x.test/feed")
    main.unflag_proxy_feed(meta, "https://x.test/feed")
    assert main.get_proxy_feed_urls(meta) == set()


def test_blank_url_not_flagged(meta):
    assert main.flag_proxy_feed(meta, "   ") is False
    assert main.get_proxy_feed_urls(meta) == set()


# --- last-resort escalation (tailscale_feeds), same shape one rung further out ---

def test_tailscale_flag_and_get(meta):
    assert main.get_tailscale_feed_urls(meta) == set()
    newly = main.flag_tailscale_feed(meta, "https://blocked.test/feed", reason="test")
    assert newly is True
    assert main.get_tailscale_feed_urls(meta) == {"https://blocked.test/feed"}


def test_tailscale_flag_is_idempotent(meta):
    assert main.flag_tailscale_feed(meta, "https://x.test/feed") is True
    assert main.flag_tailscale_feed(meta, "https://x.test/feed") is False  # already flagged


def test_tailscale_unflag(meta):
    main.flag_tailscale_feed(meta, "https://x.test/feed")
    main.unflag_tailscale_feed(meta, "https://x.test/feed")
    assert main.get_tailscale_feed_urls(meta) == set()


def test_tailscale_blank_url_not_flagged(meta):
    assert main.flag_tailscale_feed(meta, "   ") is False
    assert main.get_tailscale_feed_urls(meta) == set()


def test_tailscale_feeds_independent_of_proxy_feeds(meta):
    """The two tables are separate storage — flagging one must not touch the
    other."""
    main.flag_proxy_feed(meta, "https://proxied.test/feed")
    main.flag_tailscale_feed(meta, "https://tailscaled.test/feed")
    assert main.get_proxy_feed_urls(meta) == {"https://proxied.test/feed"}
    assert main.get_tailscale_feed_urls(meta) == {"https://tailscaled.test/feed"}


# --- FlareSolverr escalation (flaresolverr_feeds), between proxy and tailscale ---

def test_flaresolverr_flag_and_get(meta):
    assert main.get_flaresolverr_feed_urls(meta) == set()
    newly = main.flag_flaresolverr_feed(meta, "https://blocked.test/feed", reason="test")
    assert newly is True
    assert main.get_flaresolverr_feed_urls(meta) == {"https://blocked.test/feed"}


def test_flaresolverr_flag_is_idempotent(meta):
    assert main.flag_flaresolverr_feed(meta, "https://x.test/feed") is True
    assert main.flag_flaresolverr_feed(meta, "https://x.test/feed") is False  # already flagged


def test_flaresolverr_unflag(meta):
    main.flag_flaresolverr_feed(meta, "https://x.test/feed")
    main.unflag_flaresolverr_feed(meta, "https://x.test/feed")
    assert main.get_flaresolverr_feed_urls(meta) == set()


def test_flaresolverr_blank_url_not_flagged(meta):
    assert main.flag_flaresolverr_feed(meta, "   ") is False
    assert main.get_flaresolverr_feed_urls(meta) == set()


def test_flaresolverr_feeds_independent_of_the_other_two_tables(meta):
    main.flag_proxy_feed(meta, "https://proxied.test/feed")
    main.flag_flaresolverr_feed(meta, "https://challenged.test/feed")
    main.flag_tailscale_feed(meta, "https://tailscaled.test/feed")
    assert main.get_proxy_feed_urls(meta) == {"https://proxied.test/feed"}
    assert main.get_flaresolverr_feed_urls(meta) == {"https://challenged.test/feed"}
    assert main.get_tailscale_feed_urls(meta) == {"https://tailscaled.test/feed"}
