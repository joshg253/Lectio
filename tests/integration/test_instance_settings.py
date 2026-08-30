"""Instance-level settings must resolve from any context.

Instance config (Administration page) is saved into the saving admin's own
per-user app_settings, but its consumers run in other contexts — the daily
maintenance loop is a bare background thread bound to the default user, login
lockout checks run pre-auth, image-cache eviction runs in maintenance. The
regression here: maintenance_hour set via the UI was invisible to the
scheduler, so nightly maintenance (YouTube sync, VACUUMs, pruning) never ran.
"""
from __future__ import annotations

import pytest

import main
from services import tenancy

ADMIN_ID = "u_1e57adm1n0000000000000000"


class _StubUserStore:
    def __init__(self, admin_id: str):
        self._admin_id = admin_id

    def list_users(self) -> list[dict]:
        return [{"user_id": self._admin_id, "username": "admin",
                 "is_admin": True, "disabled": False}]


@pytest.fixture
def configured(tmp_path, monkeypatch):
    saved = tenancy._layout
    main.close_thread_db_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    # Stub the store before any schema init: seeding defaults resolves
    # instance settings, which iterates the (real) user store otherwise.
    monkeypatch.setattr(main, "user_store", _StubUserStore(ADMIN_ID))
    monkeypatch.setattr(main, "_ENV_MAINTENANCE_HOUR", None)
    monkeypatch.setattr(main, "_ENV_PROXY_URL", "")
    monkeypatch.setattr(main, "_ENV_TAILSCALE_URL", "")
    monkeypatch.setattr(main, "_ENV_FLARESOLVERR_URL", "")
    main.invalidate_instance_setting_cache()
    # _app_settings_cache is keyed by user_id, not by DB path — reusing ADMIN_ID
    # across tests (each with its own fresh tmp_path DB) would otherwise leak a
    # previous test's cached values into this one.
    with main._app_settings_cache_lock:
        main._app_settings_cache.clear()
    # _proxy_down_until/_tailscale_down_until are also keyed by user_id, same
    # leak risk as above.
    with main._proxy_down_lock:
        main._proxy_down_until.clear()
    with main._tailscale_down_lock:
        main._tailscale_down_until.clear()
    # And the three per-backend flagged-feeds caches (30s TTL) — keyed by
    # user_id too, so a feed flagged in one test's tmp_path DB can otherwise
    # still read back as flagged in the next test reusing ADMIN_ID.
    with main._proxy_feeds_cache_lock:
        main._proxy_feeds_cache.clear()
    with main._tailscale_feeds_cache_lock:
        main._tailscale_feeds_cache.clear()
    with main._flaresolverr_feeds_cache_lock:
        main._flaresolverr_feeds_cache.clear()
    main.ensure_meta_schema()
    main.provision_user_storage(ADMIN_ID)
    main.invalidate_instance_setting_cache()
    try:
        yield
    finally:
        main.invalidate_instance_setting_cache()
        with main._app_settings_cache_lock:
            main._app_settings_cache.clear()
        with main._proxy_down_lock:
            main._proxy_down_until.clear()
        with main._tailscale_down_lock:
            main._tailscale_down_until.clear()
        main.close_thread_db_pools()
        tenancy._layout = saved


def _set_admin_setting(key: str, value: str) -> None:
    with tenancy.user_context(ADMIN_ID):
        with main.get_meta_connection() as conn:
            main.set_setting(conn, key, value)
    main.invalidate_instance_setting_cache()


def test_admin_saved_maintenance_hour_visible_from_default_context(configured):
    assert main.get_maintenance_hour() is None  # nothing configured anywhere
    _set_admin_setting(main.SETTING_MAINTENANCE_HOUR, "3")
    # The maintenance loop runs with no user bound (default context) — it must
    # still see the admin's Instance Config value.
    assert tenancy.current_user_id() == tenancy.DEFAULT_USER_ID
    assert main.get_maintenance_hour() == 3


def test_admin_lookup_reads_db_not_just_warm_cache(configured):
    """The 3am check may run before the admin's settings cache ever loaded
    (e.g. right after a container restart) — the lookup must hit the DB."""
    _set_admin_setting(main.SETTING_MAINTENANCE_HOUR, "4")
    # Simulate a fresh process: no per-user caches, no instance TTL cache.
    with main._app_settings_cache_lock:
        main._app_settings_cache.clear()
    main.invalidate_instance_setting_cache()
    assert main.get_maintenance_hour() == 4


def test_cache_invalidation_applies_changes_immediately(configured):
    _set_admin_setting(main.SETTING_MAINTENANCE_HOUR, "3")
    assert main.get_maintenance_hour() == 3
    _set_admin_setting(main.SETTING_MAINTENANCE_HOUR, "5")  # invalidates cache
    assert main.get_maintenance_hour() == 5


def test_current_context_value_wins_over_admin(configured):
    """Single-user installs store settings under the default user; that read
    must keep working and take precedence in its own context."""
    _set_admin_setting(main.SETTING_MAINTENANCE_HOUR, "3")
    with main.get_meta_connection() as conn:  # default context
        main.set_setting(conn, main.SETTING_MAINTENANCE_HOUR, "7")
    assert main.get_maintenance_hour() == 7


def test_proxy_mode_defaults_off_everywhere(configured):
    """Nothing configured anywhere (no admin default, no per-user override, no
    env) must resolve to "off" — the proxy is opt-in, never silently on."""
    assert main.get_proxy_mode() == "off"


def test_proxy_mode_per_user_override_wins_over_admin_default(configured):
    """The admin sets the instance default; an individual user's own override
    (e.g. only the admin's own account should route through their VPN) must
    take precedence for that user without affecting the admin default."""
    _set_admin_setting(main.SETTING_PROXY_MODE, "as_needed")
    assert main.get_proxy_mode() == "as_needed"  # default-context user inherits it
    with main.get_meta_connection() as conn:  # default context = the "other" user
        main.set_setting(conn, main.SETTING_PROXY_MODE, "always")
    assert main.get_proxy_mode() == "always"


def test_proxy_mode_reverts_to_instance_default_when_override_cleared(configured):
    _set_admin_setting(main.SETTING_PROXY_MODE, "as_needed")
    with main.get_meta_connection() as conn:
        main.set_setting(conn, main.SETTING_PROXY_MODE, "always")
    assert main.get_proxy_mode() == "always"
    with main.get_meta_connection() as conn:
        main.delete_setting(conn, main.SETTING_PROXY_MODE)
    assert main.get_proxy_mode() == "as_needed"


def test_resolve_proxy_for_fetch_off_by_default(configured):
    assert main._resolve_proxy_for_fetch(ADMIN_ID, "https://example.test/feed") is None


def test_resolve_proxy_for_fetch_always_mode_returns_configured_url(configured):
    _set_admin_setting(main.SETTING_PROXY_MODE, "always")
    _set_admin_setting(main.SETTING_PROXY_URL, "socks5h://gluetun:1080")
    assert main._resolve_proxy_for_fetch(ADMIN_ID, "https://example.test/feed") == "socks5h://gluetun:1080"


def test_resolve_proxy_for_fetch_always_mode_with_no_url_configured(configured):
    """Mode set to always but no proxy URL configured must not proxy at all
    (no crash, no bare-scheme proxies dict) rather than error on every fetch."""
    _set_admin_setting(main.SETTING_PROXY_MODE, "always")
    assert main._resolve_proxy_for_fetch(ADMIN_ID, "https://example.test/feed") is None


def test_resolve_proxy_for_fetch_as_needed_only_proxies_flagged_feeds(configured):
    """as_needed must never proxy everything like always does — only feeds
    explicitly flagged by _flag_proxy_feed_on_still_blocked."""
    _set_admin_setting(main.SETTING_PROXY_MODE, "as_needed")
    _set_admin_setting(main.SETTING_PROXY_URL, "socks5h://gluetun:1080")
    assert main._resolve_proxy_for_fetch(ADMIN_ID, "https://example.test/feed") is None
    with tenancy.user_context(ADMIN_ID):
        with main.get_meta_connection() as conn:
            main.flag_proxy_feed(conn, "https://example.test/feed")
    main._invalidate_proxy_feeds_cache()
    assert main._resolve_proxy_for_fetch(ADMIN_ID, "https://example.test/feed") == "socks5h://gluetun:1080"
    # An unrelated feed stays direct even though this one is flagged.
    assert main._resolve_proxy_for_fetch(ADMIN_ID, "https://other.test/feed") is None


def test_flag_proxy_feed_on_still_blocked_is_noop_off_mode(configured):
    _set_admin_setting(main.SETTING_PROXY_MODE, "off")
    assert main._flag_proxy_feed_on_still_blocked("https://example.test/feed") is False
    with main.get_meta_connection() as conn:
        assert main.get_proxy_feed_urls(conn) == set()


def test_flag_proxy_feed_on_still_blocked_is_noop_always_mode(configured):
    """always mode doesn't need per-feed tracking — every fetch is already
    proxied, so flagging would just be dead state."""
    _set_admin_setting(main.SETTING_PROXY_MODE, "always")
    assert main._flag_proxy_feed_on_still_blocked("https://example.test/feed") is False
    with main.get_meta_connection() as conn:
        assert main.get_proxy_feed_urls(conn) == set()


def test_flag_proxy_feed_on_still_blocked_flags_in_as_needed_mode(configured):
    _set_admin_setting(main.SETTING_PROXY_MODE, "as_needed")
    assert main._flag_proxy_feed_on_still_blocked("https://example.test/feed") is True
    assert main._flag_proxy_feed_on_still_blocked("https://example.test/feed") is False  # already flagged


def test_mark_proxy_unreachable_skips_proxy_regardless_of_mode(configured):
    """A dead proxy backend must never be worse than not having one — the
    cooldown applies across every mode, not just as_needed."""
    _set_admin_setting(main.SETTING_PROXY_MODE, "always")
    _set_admin_setting(main.SETTING_PROXY_URL, "socks5h://gluetun:1080")
    assert main._resolve_proxy_for_fetch(ADMIN_ID, "https://example.test/feed") == "socks5h://gluetun:1080"
    with tenancy.user_context(ADMIN_ID):
        main._mark_backend_unreachable("https://example.test/feed")
    assert main._resolve_proxy_for_fetch(ADMIN_ID, "https://example.test/feed") is None


def test_mark_proxy_unreachable_is_per_user(configured):
    """One user's proxy going down must not affect another user's fetches."""
    _set_admin_setting(main.SETTING_PROXY_MODE, "always")
    _set_admin_setting(main.SETTING_PROXY_URL, "socks5h://gluetun:1080")
    other_uid = "u_07he9019200000000000000r"
    main.provision_user_storage(other_uid)
    with tenancy.user_context(ADMIN_ID):
        main._mark_backend_unreachable("https://example.test/feed")
    assert main._resolve_proxy_for_fetch(ADMIN_ID, "https://example.test/feed") is None
    assert main._resolve_proxy_for_fetch(other_uid, "https://example.test/feed") == "socks5h://gluetun:1080"


def test_resolve_proxy_for_fetch_respects_per_user_override(configured):
    """A user with mode=off must never be proxied even if the instance
    default is always — the whole point of the per-user override."""
    _set_admin_setting(main.SETTING_PROXY_MODE, "always")
    _set_admin_setting(main.SETTING_PROXY_URL, "socks5h://gluetun:1080")
    other_uid = "u_0the9019200000000000000r"
    main.provision_user_storage(other_uid)
    with tenancy.user_context(other_uid):
        with main.get_meta_connection() as conn:
            main.set_setting(conn, main.SETTING_PROXY_MODE, "off")
    assert main._resolve_proxy_for_fetch(other_uid, "https://example.test/feed") is None
    assert main._resolve_proxy_for_fetch(ADMIN_ID, "https://example.test/feed") == "socks5h://gluetun:1080"


# --- last-resort backend (Tailscale) — rides the same proxy_mode, one rung
#     further out than the primary proxy ---

def test_tailscale_url_defaults_empty(configured):
    assert main.get_tailscale_url() == ""


def test_flag_tailscale_feed_on_still_blocked_is_noop_without_url_configured(configured):
    """as_needed mode alone is not the opt-in — an unconfigured last-resort URL
    is, same as the primary proxy's own off-by-default gate."""
    _set_admin_setting(main.SETTING_PROXY_MODE, "as_needed")
    assert main._flag_tailscale_feed_on_still_blocked("https://example.test/feed") is False
    with main.get_meta_connection() as conn:
        assert main.get_tailscale_feed_urls(conn) == set()


def test_flag_tailscale_feed_on_still_blocked_is_noop_always_mode(configured):
    """Never reachable in always mode — that would route every fetch through
    the home IP, exactly what "last resort" rules out."""
    _set_admin_setting(main.SETTING_PROXY_MODE, "always")
    _set_admin_setting(main.SETTING_TAILSCALE_URL, "socks5h://tailscale:1080")
    assert main._flag_tailscale_feed_on_still_blocked("https://example.test/feed") is False


def test_flag_tailscale_feed_on_still_blocked_flags_in_as_needed_mode(configured):
    _set_admin_setting(main.SETTING_PROXY_MODE, "as_needed")
    _set_admin_setting(main.SETTING_TAILSCALE_URL, "socks5h://tailscale:1080")
    assert main._flag_tailscale_feed_on_still_blocked("https://example.test/feed") is True
    assert main._flag_tailscale_feed_on_still_blocked("https://example.test/feed") is False  # already flagged


def test_resolve_proxy_for_fetch_prefers_tailscale_over_proxy_when_both_flagged(configured):
    """Tailscale only ever gets flagged one rung past the proxy (the proxy was
    already tried and failed) — so if both flags are set, the fetch that
    already went through the proxy and is being retried again must go through
    the last-resort backend, not back through the proxy that just failed it."""
    _set_admin_setting(main.SETTING_PROXY_MODE, "as_needed")
    _set_admin_setting(main.SETTING_PROXY_URL, "socks5h://gluetun:1080")
    _set_admin_setting(main.SETTING_TAILSCALE_URL, "socks5h://tailscale:1080")
    with tenancy.user_context(ADMIN_ID):
        with main.get_meta_connection() as conn:
            main.flag_proxy_feed(conn, "https://example.test/feed")
            main.flag_tailscale_feed(conn, "https://example.test/feed")
    main._invalidate_proxy_feeds_cache()
    main._invalidate_tailscale_feeds_cache()
    assert main._resolve_proxy_for_fetch(ADMIN_ID, "https://example.test/feed") == "socks5h://tailscale:1080"


def test_resolve_proxy_for_fetch_never_uses_tailscale_in_always_mode(configured):
    """always mode has no per-feed tracking at all — a stray tailscale flag
    (e.g. left over from a mode switch) must still never take effect there."""
    _set_admin_setting(main.SETTING_PROXY_MODE, "always")
    _set_admin_setting(main.SETTING_PROXY_URL, "socks5h://gluetun:1080")
    _set_admin_setting(main.SETTING_TAILSCALE_URL, "socks5h://tailscale:1080")
    with tenancy.user_context(ADMIN_ID):
        with main.get_meta_connection() as conn:
            main.flag_tailscale_feed(conn, "https://example.test/feed")
    main._invalidate_tailscale_feeds_cache()
    assert main._resolve_proxy_for_fetch(ADMIN_ID, "https://example.test/feed") == "socks5h://gluetun:1080"


def test_mark_backend_unreachable_marks_only_the_active_backend(configured):
    """A dead last-resort backend must not also pause the primary proxy —
    they have very different reliability profiles and one blipping is not
    evidence the other is down too."""
    _set_admin_setting(main.SETTING_PROXY_MODE, "as_needed")
    _set_admin_setting(main.SETTING_PROXY_URL, "socks5h://gluetun:1080")
    _set_admin_setting(main.SETTING_TAILSCALE_URL, "socks5h://tailscale:1080")
    with tenancy.user_context(ADMIN_ID):
        with main.get_meta_connection() as conn:
            main.flag_proxy_feed(conn, "https://tailscale-feed.test/feed")
            main.flag_tailscale_feed(conn, "https://tailscale-feed.test/feed")
    main._invalidate_proxy_feeds_cache()
    main._invalidate_tailscale_feeds_cache()
    assert main._resolve_proxy_for_fetch(ADMIN_ID, "https://tailscale-feed.test/feed") == "socks5h://tailscale:1080"

    with tenancy.user_context(ADMIN_ID):
        main._mark_backend_unreachable("https://tailscale-feed.test/feed")

    # The last-resort backend is down for this feed...
    assert main._resolve_proxy_for_fetch(ADMIN_ID, "https://tailscale-feed.test/feed") is None
    # ...but the primary proxy is untouched, for a feed that only ever uses it.
    with tenancy.user_context(ADMIN_ID):
        with main.get_meta_connection() as conn:
            main.flag_proxy_feed(conn, "https://proxy-only-feed.test/feed")
    main._invalidate_proxy_feeds_cache()
    assert main._resolve_proxy_for_fetch(ADMIN_ID, "https://proxy-only-feed.test/feed") == "socks5h://gluetun:1080"


# --- FlareSolverr — rides the same proxy_mode, between the proxy and Tailscale ---

def test_flaresolverr_url_defaults_empty(configured):
    assert main.get_flaresolverr_url() == ""


def test_flag_flaresolverr_feed_on_still_blocked_is_noop_without_url_configured(configured):
    _set_admin_setting(main.SETTING_PROXY_MODE, "as_needed")
    assert main._flag_flaresolverr_feed_on_still_blocked("https://example.test/feed") is False
    with main.get_meta_connection() as conn:
        assert main.get_flaresolverr_feed_urls(conn) == set()


def test_flag_flaresolverr_feed_on_still_blocked_is_noop_always_mode(configured):
    """Never reachable in always mode — that would spin up real Chrome for
    every single fetch, the thing per-feed escalation exists to avoid."""
    _set_admin_setting(main.SETTING_PROXY_MODE, "always")
    _set_admin_setting(main.SETTING_FLARESOLVERR_URL, "http://flaresolverr:8191/v1")
    assert main._flag_flaresolverr_feed_on_still_blocked("https://example.test/feed") is False


def test_flag_flaresolverr_feed_on_still_blocked_flags_in_as_needed_mode(configured):
    _set_admin_setting(main.SETTING_PROXY_MODE, "as_needed")
    _set_admin_setting(main.SETTING_FLARESOLVERR_URL, "http://flaresolverr:8191/v1")
    assert main._flag_flaresolverr_feed_on_still_blocked("https://example.test/feed") is True
    assert main._flag_flaresolverr_feed_on_still_blocked("https://example.test/feed") is False  # already flagged


def test_resolve_flaresolverr_for_fetch_returns_endpoint_and_stacked_proxy(configured):
    """Confirmed empirically (2026-08-30) against a real Cloudflare-protected
    feed: FlareSolverr alone (bare VPS IP) failed outright; stacked with the
    primary proxy, it solved the challenge cleanly. So the primary proxy is
    always the stack partner when one is configured — with its scheme
    normalized for Chrome (socks5h:// -> socks5://; see
    _normalize_proxy_scheme_for_flaresolverr), NOT the raw configured value:
    also confirmed empirically that socks5h:// silently broke FlareSolverr's
    own proxy config and it fell through to a bare connection-error page."""
    _set_admin_setting(main.SETTING_PROXY_MODE, "as_needed")
    _set_admin_setting(main.SETTING_PROXY_URL, "socks5h://gluetun:1080")
    _set_admin_setting(main.SETTING_FLARESOLVERR_URL, "http://flaresolverr:8191/v1")
    with tenancy.user_context(ADMIN_ID):
        with main.get_meta_connection() as conn:
            main.flag_proxy_feed(conn, "https://example.test/feed")
            main.flag_flaresolverr_feed(conn, "https://example.test/feed")
    main._invalidate_proxy_feeds_cache()
    main._invalidate_flaresolverr_feeds_cache()
    result = main._resolve_flaresolverr_for_fetch(ADMIN_ID, "https://example.test/feed")
    assert result == ("http://flaresolverr:8191/v1", "socks5://gluetun:1080")


def test_normalize_proxy_scheme_for_flaresolverr():
    f = main._normalize_proxy_scheme_for_flaresolverr
    assert f("socks5h://gluetun:1080") == "socks5://gluetun:1080"
    assert f("socks5://gluetun:1080") == "socks5://gluetun:1080"  # already plain, untouched
    assert f("http://gluetun:8888") == "http://gluetun:8888"  # non-SOCKS scheme, untouched


def test_resolve_flaresolverr_for_fetch_none_when_not_flagged(configured):
    _set_admin_setting(main.SETTING_PROXY_MODE, "as_needed")
    _set_admin_setting(main.SETTING_FLARESOLVERR_URL, "http://flaresolverr:8191/v1")
    assert main._resolve_flaresolverr_for_fetch(ADMIN_ID, "https://example.test/feed") is None


def test_resolve_proxy_for_fetch_stands_down_for_a_flaresolverr_active_feed(configured):
    """The primary proxy hook must NOT also fire for a feed FlareSolverr is
    handling — otherwise the request to FlareSolverr's own container gets
    routed through the primary proxy's session.proxies by mistake. Stacking
    happens inside FlareSolverr's own request body instead."""
    _set_admin_setting(main.SETTING_PROXY_MODE, "as_needed")
    _set_admin_setting(main.SETTING_PROXY_URL, "socks5h://gluetun:1080")
    _set_admin_setting(main.SETTING_FLARESOLVERR_URL, "http://flaresolverr:8191/v1")
    with tenancy.user_context(ADMIN_ID):
        with main.get_meta_connection() as conn:
            main.flag_proxy_feed(conn, "https://example.test/feed")
            main.flag_flaresolverr_feed(conn, "https://example.test/feed")
    main._invalidate_proxy_feeds_cache()
    main._invalidate_flaresolverr_feeds_cache()
    assert main._resolve_proxy_for_fetch(ADMIN_ID, "https://example.test/feed") is None


def test_tailscale_outranks_flaresolverr_when_both_flagged(configured):
    """Tailscale is only ever flagged after FlareSolverr also failed, so if
    both flags exist, the fetch must go through the deepest tier reached, not
    fall back to an earlier one that already proved insufficient."""
    _set_admin_setting(main.SETTING_PROXY_MODE, "as_needed")
    _set_admin_setting(main.SETTING_PROXY_URL, "socks5h://gluetun:1080")
    _set_admin_setting(main.SETTING_FLARESOLVERR_URL, "http://flaresolverr:8191/v1")
    _set_admin_setting(main.SETTING_TAILSCALE_URL, "socks5h://tailscale:1080")
    with tenancy.user_context(ADMIN_ID):
        with main.get_meta_connection() as conn:
            main.flag_proxy_feed(conn, "https://example.test/feed")
            main.flag_flaresolverr_feed(conn, "https://example.test/feed")
            main.flag_tailscale_feed(conn, "https://example.test/feed")
    main._invalidate_proxy_feeds_cache()
    main._invalidate_flaresolverr_feeds_cache()
    main._invalidate_tailscale_feeds_cache()
    assert main._resolve_proxy_for_fetch(ADMIN_ID, "https://example.test/feed") == "socks5h://tailscale:1080"
    assert main._resolve_flaresolverr_for_fetch(ADMIN_ID, "https://example.test/feed") is None
