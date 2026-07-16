"""POST /instapaper/import — Instapaper CSV export into Saved Items.

Verifies the orchestration around the pure parser: entries land in the Saved
Articles feed, Instapaper's Archive maps to saved_entries.archived_at, custom
folders / the Starred flag become manual tags, content fetch is deferred to
the starred-archive worker (enqueued, not fetched inline), and re-import is
idempotent.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

import main
from services import tenancy
from services.saved_articles import SAVED_FEED_URL

CSV = (
    "URL,Title,Selection,Folder,Timestamp\n"
    "https://ex.test/unread,Unread One,,Unread,1600000000\n"
    "https://ex.test/arch,Archived One,,Archive,1600000001\n"
    "https://ex.test/folder,Foldered,,Read Later,1600000002\n"
    "https://ex.test/star,Liked,,Starred,1600000003\n"
)


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "AUTH_ENABLED", False)
    saved = tenancy._layout
    saved_store = main.user_store
    main.close_thread_db_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    main.user_store = None
    # Fetching real pages is the archive worker's job; record the enqueue
    # instead of doing network I/O in the test.
    enqueued: list[tuple[str, str]] = []
    monkeypatch.setattr(
        main.starred_archive_service, "enqueue_archive",
        lambda feed_url, entry_id: enqueued.append((feed_url, entry_id)),
    )
    try:
        yield enqueued
    finally:
        main.user_store = saved_store
        main.close_thread_db_pools()
        tenancy._layout = saved


def _csrf(client: TestClient) -> str:
    import base64
    import json

    from itsdangerous import TimestampSigner

    client.get("/healthz")  # establish session + token
    cookie = client.cookies.get("session")
    signer = TimestampSigner(main.SESSION_SECRET_KEY)
    decoded = signer.unsign(cookie, max_age=main.SESSION_MAX_AGE_SECONDS)
    return json.loads(base64.b64decode(decoded))["csrf_token"]


def _upload(csv: str):
    client = TestClient(main.app)
    token = _csrf(client)
    return client.post(
        "/instapaper/import",
        files={"instapaper_file": ("instapaper-export.csv", csv, "text/csv")},
        headers={"X-CSRF-Token": token},
        follow_redirects=False,
    )


def _manual_tags(url: str) -> list[str]:
    with main.get_reader() as reader:
        entry = reader.get_entry((SAVED_FEED_URL, url), None)
        return main.get_manual_tags_for_resource(reader, entry.resource_id)


def test_import_creates_saved_entries(configured):
    r = _upload(CSV)
    assert r.status_code == 303
    with main.get_meta_connection() as conn:
        rows = dict(conn.execute(
            "SELECT entry_id, archived_at FROM saved_entries"
        ).fetchall())
    assert set(rows) == {
        "https://ex.test/unread", "https://ex.test/arch",
        "https://ex.test/folder", "https://ex.test/star",
    }
    # Only the Archive-folder item is archived.
    assert rows["https://ex.test/arch"] is not None
    assert rows["https://ex.test/unread"] is None


def test_import_maps_folders_and_star_to_tags(configured):
    _upload(CSV)
    assert _manual_tags("https://ex.test/folder") == ["read-later"]
    assert _manual_tags("https://ex.test/star") == ["starred"]
    assert _manual_tags("https://ex.test/unread") == []


def test_import_defers_content_to_archive_worker(configured):
    enqueued = configured
    _upload(CSV)
    # Every imported bookmark is queued for offline content fetch, none fetched
    # inline (no content stored yet).
    assert len(enqueued) == 4
    with main.get_reader() as reader:
        entry = reader.get_entry((SAVED_FEED_URL, "https://ex.test/unread"))
    assert entry.title == "Unread One"
    assert not entry.content  # body arrives later via the worker


def test_reimport_is_idempotent(configured):
    _upload(CSV)
    r = _upload(CSV)
    assert r.status_code == 303
    with main.get_meta_connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM saved_entries").fetchone()[0]
    assert count == 4  # no duplicates on re-upload


def test_empty_or_bad_csv_reports_nothing_imported(configured):
    r = _upload("not a csv")
    assert r.status_code == 303
    assert "No+bookmarks" in r.headers["location"]
    with main.get_meta_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM saved_entries").fetchone()[0] == 0
