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
    main.invalidate_instance_setting_cache()
    main.ensure_meta_schema()
    main.provision_user_storage(ADMIN_ID)
    main.invalidate_instance_setting_cache()
    try:
        yield
    finally:
        main.invalidate_instance_setting_cache()
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
