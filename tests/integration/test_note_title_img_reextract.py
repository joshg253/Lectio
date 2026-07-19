"""Small UX fixes: Global Note live-pull endpoint, and the saved-article
Re-fetch-content endpoint (force re-extract of a bad capture)."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
from services import tenancy


def _reset_reader_pool():
    main.close_thread_db_pools()


@pytest.fixture
def tenant(tmp_path):
    saved = tenancy._layout
    _reset_reader_pool()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    # The app-settings cache is keyed by (default) user id and survives across
    # tests; clear it so each test sees its own fresh DB.
    with main._app_settings_cache_lock:
        main._app_settings_cache.clear()
    try:
        yield
    finally:
        with main._app_settings_cache_lock:
            main._app_settings_cache.clear()
        _reset_reader_pool()
        tenancy._layout = saved


# --- Global Note GET (live-pull on modal open) ---------------------------------

def test_global_note_get_returns_stored_value(tenant):
    with main.get_meta_connection() as conn:
        main.set_setting(conn, main.GLOBAL_NOTE_SETTING_KEY, "remember the milk")

    app = FastAPI()
    app.get("/settings/global-note")(main.get_global_note_setting)
    with TestClient(app) as client:
        r = client.get("/settings/global-note")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "note_text": "remember the milk"}


def test_global_note_get_empty_when_unset(tenant):
    app = FastAPI()
    app.get("/settings/global-note")(main.get_global_note_setting)
    with TestClient(app) as client:
        r = client.get("/settings/global-note")
    assert r.json()["note_text"] == ""


# --- Saved-article Re-fetch content --------------------------------------------

def _refresh_app():
    app = FastAPI()
    app.post("/articles/refresh-content")(main.refresh_saved_article_content)
    return app


def test_refetch_rejects_non_saved_feed(tenant):
    with TestClient(_refresh_app()) as client:
        r = client.post(
            "/articles/refresh-content",
            data={"feed_url": "https://example.com/feed", "entry_id": "e1"},
        )
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_refetch_forces_reextract_for_saved_entry(tenant, monkeypatch):
    calls: list[tuple] = []

    def fake_save(url, extract=None, refresh_content=False):
        calls.append((url, extract, refresh_content))
        return {"ok": True, "refreshed": True, "extracted": True, "title": "Clean Title",
                "duplicate": True, "feed_url": "lectio:saved", "entry_id": url}

    monkeypatch.setattr(main, "_save_article_for_current_user", fake_save)

    url = "https://schacon.github.io/git/everyday.html"
    with TestClient(_refresh_app()) as client:
        r = client.post(
            "/articles/refresh-content",
            data={"feed_url": "lectio:saved", "entry_id": url},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["refreshed"] is True
    # Forced re-extract: called with the source URL and refresh_content=True.
    assert calls == [(url, None, True)]


def test_refetch_surfaces_failure(tenant, monkeypatch):
    monkeypatch.setattr(
        main, "_save_article_for_current_user",
        lambda url, extract=None, refresh_content=False: {"ok": False, "error": "boom"},
    )
    with TestClient(_refresh_app()) as client:
        r = client.post(
            "/articles/refresh-content",
            data={"feed_url": "lectio:saved", "entry_id": "https://x.test/a"},
        )
    assert r.status_code == 400
    assert r.json()["error"] == "boom"
