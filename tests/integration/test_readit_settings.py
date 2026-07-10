"""Readit integration setting: the per-user bookmarklet token gates the
share-menu button (readit_configured) and is treated as a secret."""
from __future__ import annotations

import pytest

import main
from services import tenancy


@pytest.fixture
def configured(tmp_path):
    saved = tenancy._layout
    main._reader_thread_local.pool = None
    main._meta_conn_local.pool = None
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
        main._reader_thread_local.pool = None
        main._meta_conn_local.pool = None
        tenancy._layout = saved


def test_readit_configured_toggles_on_token(configured):
    assert main.is_readit_configured() is False
    with main.get_meta_connection() as conn:
        main.set_setting(conn, main.SETTING_READIT_TOKEN, "tok-123")
        conn.commit()
    assert main.is_readit_configured() is True


def test_readit_token_is_sensitive_and_allowed():
    """The save route must accept the key but never let a masked/blank re-save
    wipe the stored token (the _keep_existing_sensitive contract)."""
    assert main._keep_existing_sensitive(main.SETTING_READIT_TOKEN, "", {main.SETTING_READIT_TOKEN})
    assert main._keep_existing_sensitive(main.SETTING_READIT_TOKEN, "••••abcd", {main.SETTING_READIT_TOKEN})
    assert not main._keep_existing_sensitive(main.SETTING_READIT_TOKEN, "new-token", {main.SETTING_READIT_TOKEN})
