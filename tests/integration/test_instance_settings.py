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
    main.invalidate_instance_setting_cache()
    # _app_settings_cache is keyed by user_id, not by DB path — reusing ADMIN_ID
    # across tests (each with its own fresh tmp_path DB) would otherwise leak a
    # previous test's cached values into this one.
    with main._app_settings_cache_lock:
        main._app_settings_cache.clear()
    # _proxy_down_until is also keyed by user_id, same leak risk as above.
    with main._proxy_down_lock:
        main._proxy_down_until.clear()
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
        main._mark_proxy_unreachable()
    assert main._resolve_proxy_for_fetch(ADMIN_ID, "https://example.test/feed") is None


def test_mark_proxy_unreachable_is_per_user(configured):
    """One user's proxy going down must not affect another user's fetches."""
    _set_admin_setting(main.SETTING_PROXY_MODE, "always")
    _set_admin_setting(main.SETTING_PROXY_URL, "socks5h://gluetun:1080")
    other_uid = "u_07he9019200000000000000r"
    main.provision_user_storage(other_uid)
    with tenancy.user_context(ADMIN_ID):
        main._mark_proxy_unreachable()
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
