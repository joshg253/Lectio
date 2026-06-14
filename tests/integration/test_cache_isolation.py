"""Per-user isolation of in-memory caches (the cross-user data-bleed bug).

In multi mode the module-level caches must partition by tenancy user so one
user's cached folder tree / settings / counts never show up for another.
"""
from __future__ import annotations

import main
from services import tenancy


def test_per_user_dict_isolates_by_user():
    c = main._PerUserDict()
    with tenancy.user_context("u_alice"):
        c["k"] = "alice"
        c.update({"x": 1})
        assert c.get("k") == "alice"
        assert bool(c) and "k" in c and len(c) == 2
    with tenancy.user_context("u_bob"):
        # Bob sees an empty cache — no bleed from Alice.
        assert c.get("k") is None
        assert not bool(c) and "k" not in c and len(c) == 0
        c["k"] = "bob"
    with tenancy.user_context("u_alice"):
        assert c["k"] == "alice"  # Alice unaffected by Bob's write
    # Default context is its own partition too.
    assert c.get("k") is None


def test_per_user_dict_clear_is_scoped():
    c = main._PerUserDict()
    with tenancy.user_context("u_a"):
        c["k"] = 1
    with tenancy.user_context("u_b"):
        c["k"] = 2
        c.clear()
        assert not bool(c)
    with tenancy.user_context("u_a"):
        assert c.get("k") == 1  # clearing b didn't touch a


def test_email_from_per_user_resend_key_shared(monkeypatch):
    uid = tenancy.current_user_id()
    monkeypatch.setattr(main, "_ENV_RESEND_FROM", "instance@example.com")
    monkeypatch.setattr(main, "_ENV_RESEND_API_KEY", "shared-key")
    monkeypatch.setattr(main, "_app_settings_cache", {uid: {}})  # loaded, empty
    # email_from does NOT fall back to env (per-user identity) ...
    assert main.get_resend_from() == ""
    # ... but the Resend API key keeps its instance-shared env fallback.
    assert main.get_resend_api_key() == "shared-key"
    # A per-user email_from is honored.
    monkeypatch.setattr(main, "_app_settings_cache", {uid: {main.SETTING_EMAIL_FROM: "me@example.com"}})
    assert main.get_resend_from() == "me@example.com"


def test_app_settings_cache_isolated_per_user(tmp_path, monkeypatch):
    # Point tenancy at a throwaway layout so we don't touch the shared test dir.
    saved = tenancy._layout
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "lectio_reader.sqlite",
        legacy_meta=tmp_path / "lectio_meta.sqlite3",
        legacy_starred=tmp_path / "lectio_starred_archive.sqlite",
    )
    # Isolate the module cache so other tests aren't affected.
    monkeypatch.setattr(main, "_app_settings_cache", {})
    try:
        for uid in ("u_one", "u_two"):
            tenancy.ensure_user_data_dir(uid)
            with tenancy.user_context(uid):
                main.ensure_meta_schema()
        with tenancy.user_context("u_one"):
            with main.get_meta_connection() as cn:
                main.set_setting(cn, "profile_name", "One")
        with tenancy.user_context("u_two"):
            with main.get_meta_connection() as cn:
                assert main.get_setting(cn, "profile_name") is None  # no bleed
                main.set_setting(cn, "profile_name", "Two")
        with tenancy.user_context("u_one"):
            with main.get_meta_connection() as cn:
                assert main.get_setting(cn, "profile_name") == "One"
    finally:
        tenancy._layout = saved
        # Drop the per-user meta connections opened above so later tests are clean.
        if hasattr(main._meta_conn_local, "pool"):
            for c in main._meta_conn_local.pool.values():
                try:
                    c.close()
                except Exception:
                    pass
            main._meta_conn_local.pool.clear()
