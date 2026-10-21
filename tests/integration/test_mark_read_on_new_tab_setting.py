"""The per-user "mark read when opened in a new tab" setting: off by default, and on the /settings/all allowlist."""

from __future__ import annotations

import pytest
from _tenancy_helpers import configure_test_tenancy

import main
import routes.settings
from services import tenancy


@pytest.fixture
def configured(tmp_path):
    saved = tenancy._layout
    main.close_thread_db_pools()
    configure_test_tenancy(tmp_path)
    main.ensure_meta_schema()
    with main._app_settings_cache_lock:
        main._app_settings_cache.clear()
    try:
        yield
    finally:
        with main._app_settings_cache_lock:
            main._app_settings_cache.clear()
        main.close_thread_db_pools()
        tenancy._layout = saved


def test_off_by_default(configured):
    assert main.mark_read_on_new_tab_enabled() is False


def test_on_when_set(configured):
    with main.get_meta_connection() as conn:
        main.set_setting(conn, main.SETTING_MARK_READ_ON_NEW_TAB, "1")
    assert main.mark_read_on_new_tab_enabled() is True


def test_settings_all_accepts_the_key():
    """/settings/all silently drops keys outside its allowlist, so the checkbox would save nothing."""
    import inspect

    assert "SETTING_MARK_READ_ON_NEW_TAB," in inspect.getsource(routes.settings).split("_ALLOWED = {", 1)[1].split("}", 1)[0]
