"""instapaper automation rule: persistence + the after-refresh save path."""
from __future__ import annotations

import datetime as dt

import pytest

import main
from services import tenancy

FEED = "https://example.test/feed"


def _reset_pools():
    main._reader_thread_local.pool = None
    main._meta_conn_local.pool = None


@pytest.fixture
def env(tmp_path, monkeypatch):
    saved = tenancy._layout
    _reset_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    with main.get_meta_connection() as conn:
        main.set_setting(conn, main.SETTING_INSTAPAPER_USERNAME, "me@example.com")
        main.set_setting(conn, main.SETTING_INSTAPAPER_PASSWORD, "pw")
    reader = main.get_reader()
    reader.add_feed(FEED, allow_invalid_url=True)
    reader.add_entry({"feed_url": FEED, "id": "m1", "title": "Big metal show",
                      "link": "https://example.test/a", "summary": "x",
                      "published": dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)})
    reader.add_entry({"feed_url": FEED, "id": "m2", "title": "Cooking tips",
                      "link": "https://example.test/b", "summary": "y",
                      "published": dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)})
    try:
        yield
    finally:
        _reset_pools()
        with main._app_settings_cache_lock:
            main._app_settings_cache.clear()
        tenancy._layout = saved


def test_rule_persists(env):
    with main.get_meta_connection() as conn:
        main.add_highlight_keyword(conn, "feed", FEED, "metal", "yellow",
                                   rule_type="instapaper", enabled=1)
        r = main.get_highlight_keywords(conn)[0]
    assert r["type"] == "instapaper"
    assert r["keyword"] == "metal"


def test_after_refresh_saves_matching_entries(env, monkeypatch):
    saved = []
    monkeypatch.setattr(main, "_instapaper_save_url",
                        lambda u, p, url, title: (saved.append((url, title)) or (True, None)))
    with main.get_meta_connection() as conn:
        main.add_highlight_keyword(conn, "feed", FEED, "metal", "yellow",
                                   rule_type="instapaper", enabled=1, search_in="title")
    main._run_instapaper_rules_after_refresh({FEED})
    # Only the title matching "metal" is saved.
    assert saved == [("https://example.test/a", "Big metal show")]


def test_blank_keyword_saves_all_in_scope(env, monkeypatch):
    saved = []
    monkeypatch.setattr(main, "_instapaper_save_url",
                        lambda u, p, url, title: (saved.append(url) or (True, None)))
    with main.get_meta_connection() as conn:
        main.add_highlight_keyword(conn, "feed", FEED, "", "yellow",
                                   rule_type="instapaper", enabled=1)
    main._run_instapaper_rules_after_refresh({FEED})
    # Blank keyword → every entry in scope is saved.
    assert sorted(saved) == ["https://example.test/a", "https://example.test/b"]


def test_not_configured_is_noop(env, monkeypatch):
    with main.get_meta_connection() as conn:
        main.delete_setting(conn, main.SETTING_INSTAPAPER_PASSWORD)
    called = []
    monkeypatch.setattr(main, "_instapaper_save_url",
                        lambda *a: called.append(a) or (True, None))
    with main.get_meta_connection() as conn:
        main.add_highlight_keyword(conn, "feed", FEED, "metal", "yellow",
                                   rule_type="instapaper", enabled=1)
    main._run_instapaper_rules_after_refresh({FEED})
    assert called == []
