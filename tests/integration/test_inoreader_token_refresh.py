"""A dead Inoreader refresh token used to leave inoreader_connected() reporting
True forever (it only checked whether a token was *stored*, not whether it still
works), hiding the Connect button behind a stale "Connected." status. On a
refresh failure caused by Inoreader permanently rejecting the token
(invalid_grant), get_inoreader_token now clears the stored tokens so
inoreader_connected() flips back to False. A transient failure (network blip,
Inoreader outage) must NOT clear a still-valid token."""
from __future__ import annotations

import pytest

import main
from services import inoreader as inoreader_service
from services import tenancy


def _reset_pools():
    main.close_thread_db_pools()


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


def _seed_tokens():
    with main.get_meta_connection() as conn:
        main.set_setting(conn, main.SETTING_INOREADER_ACCESS_TOKEN, "stale-access")
        main.set_setting(conn, main.SETTING_INOREADER_REFRESH_TOKEN, "dead-refresh")
        main.set_setting(conn, main.SETTING_INOREADER_TOKEN_EXPIRES_AT, "0")


def test_invalid_grant_clears_stored_tokens(configured, monkeypatch):
    _seed_tokens()
    monkeypatch.setattr(main, "get_inoreader_credentials", lambda: ("cid", "secret"))

    def _raise(*a, **k):
        raise RuntimeError('token refresh failed: HTTP 400: {"error":"invalid_grant"}')

    monkeypatch.setattr(inoreader_service, "refresh_access_token", _raise)

    assert main.inoreader_connected() is True
    assert main.get_inoreader_token() == ""
    assert main.inoreader_connected() is False


def test_transient_failure_keeps_stored_tokens(configured, monkeypatch):
    _seed_tokens()
    monkeypatch.setattr(main, "get_inoreader_credentials", lambda: ("cid", "secret"))

    def _raise(*a, **k):
        raise RuntimeError("connection timed out")

    monkeypatch.setattr(inoreader_service, "refresh_access_token", _raise)

    assert main.get_inoreader_token() == ""
    assert main.inoreader_connected() is True
