"""The entry pane doesn't refresh after a background auto-refetch-on-tag finishes.

_maybe_autofetch_on_keep (main.py) re-fetches a thin stub in a background thread
that starts *after* the star/tag request already returned -- the pane has
already rendered the stale stub by the time the thread lands. These tests cover
the fix: the function now reports whether it kicked off a job, the star/tag
routes surface that as `autofetch_pending`, and a small per-entry status route
lets the client poll for the result -- see Plan.md and docs/architecture/views.md
for the shipped fix shape this replaces.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
import routes.entries
from services import tenancy

FEED = "https://example.test/feed"
ENTRY = "e0"
LINK = "https://example.test/post"


class _FakeThread:
    """Captures Thread(target=...) without running it -- see
    test_feed_strategy_routes.py for the precedent this mirrors."""

    last = None

    def __init__(self, target=None, args=(), daemon=None, **_kwargs):
        self.target = target
        _FakeThread.last = self

    def start(self):
        pass


@pytest.fixture
def thin_entry(tmp_path, monkeypatch):
    saved = tenancy._layout
    main.close_thread_db_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    with main.get_reader() as reader:
        reader.add_feed(FEED, exist_ok=True)
        # No content/summary at all -- _archived_copy_is_plausible("") is False,
        # so this reads as a stub worth auto-refetching.
        reader.add_entry({"feed_url": FEED, "id": ENTRY, "title": "post", "link": LINK})
    main._autofetch_jobs.clear()
    monkeypatch.setattr(main.threading, "Thread", _FakeThread)
    _FakeThread.last = None
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _run_pending_job():
    """Synchronously execute the background thread _maybe_autofetch_on_keep
    just queued, exactly as _FakeThread would once .start() actually ran it."""
    assert _FakeThread.last is not None, "no autofetch thread was queued"
    _FakeThread.last.target()


def test_returns_false_and_no_job_when_the_stored_copy_is_already_plausible(thin_entry, monkeypatch):
    monkeypatch.setattr(main, "_archived_copy_is_plausible", lambda html: True)
    assert main._maybe_autofetch_on_keep(FEED, ENTRY) is False
    assert main._autofetch_jobs.get((FEED, ENTRY)) is None
    assert _FakeThread.last is None


def test_kicks_off_a_job_and_the_status_route_reflects_it_running_then_done(thin_entry, monkeypatch):
    monkeypatch.setattr(main, "_refresh_captured_article_for_current_user", lambda f, e: {"ok": True})

    assert main._maybe_autofetch_on_keep(FEED, ENTRY) is True
    job = main._autofetch_jobs.get((FEED, ENTRY))
    assert job is not None and job["running"] is True

    app = FastAPI()
    app.get("/entries/autofetch-status")(routes.entries.entry_autofetch_status)
    with TestClient(app) as client:
        r = client.get("/entries/autofetch-status", params={"feed_url": FEED, "entry_id": ENTRY})
        assert r.json() == {"ok": True, "pending": True, "done": False, "success": None}

        _run_pending_job()

        r = client.get("/entries/autofetch-status", params={"feed_url": FEED, "entry_id": ENTRY})
        assert r.json() == {"ok": True, "pending": False, "done": True, "success": True}


def test_status_route_is_a_harmless_noop_for_an_entry_with_no_job(thin_entry):
    app = FastAPI()
    app.get("/entries/autofetch-status")(routes.entries.entry_autofetch_status)
    with TestClient(app) as client:
        r = client.get("/entries/autofetch-status", params={"feed_url": FEED, "entry_id": "never-tagged"})
        assert r.json() == {"ok": True, "pending": False, "done": False, "success": None}


def test_a_failed_refetch_is_reported_as_done_but_not_successful(thin_entry, monkeypatch):
    monkeypatch.setattr(main, "_refresh_captured_article_for_current_user", lambda f, e: {"ok": False, "error": "blocked"})
    monkeypatch.setattr(main, "_mark_autofetch_host_failed", lambda host: None)

    assert main._maybe_autofetch_on_keep(FEED, ENTRY) is True
    _run_pending_job()

    job = main._autofetch_jobs.get((FEED, ENTRY))
    assert job["running"] is False
    assert job["ok"] is False


def test_a_second_call_while_the_first_job_is_still_running_does_not_spawn_another_thread(thin_entry, monkeypatch):
    monkeypatch.setattr(main, "_refresh_captured_article_for_current_user", lambda f, e: {"ok": True})

    assert main._maybe_autofetch_on_keep(FEED, ENTRY) is True
    first_thread = _FakeThread.last
    first_job = main._autofetch_jobs.get((FEED, ENTRY))

    # The job is still "running" (nobody has executed the queued thread yet),
    # so a second keep signal on the same stub must not race a second fetch.
    assert main._maybe_autofetch_on_keep(FEED, ENTRY) is True
    assert _FakeThread.last is first_thread  # no new thread was queued
    assert main._autofetch_jobs.get((FEED, ENTRY)) is first_job  # same job object


def test_star_route_response_flags_autofetch_pending_for_a_stub(thin_entry, monkeypatch):
    monkeypatch.setattr(main, "_refresh_captured_article_for_current_user", lambda f, e: {"ok": True})
    app = FastAPI()
    app.post("/entries/saved")(main.toggle_entry_saved)
    with TestClient(app) as client:
        r = client.post(
            "/entries/saved",
            data={"folder_id": "0", "feed_url": FEED, "entry_id": ENTRY, "saved": "1"},
            headers={"X-Requested-With": "lectio-post-save-toggle"},
        )
    assert r.json()["autofetch_pending"] is True


def test_tags_route_response_flags_autofetch_pending_for_a_stub(thin_entry, monkeypatch):
    monkeypatch.setattr(main, "_refresh_captured_article_for_current_user", lambda f, e: {"ok": True})
    app = FastAPI()
    app.post("/entries/tags")(routes.entries.set_entry_manual_tags)
    with TestClient(app) as client:
        r = client.post(
            "/entries/tags",
            data={"folder_id": "0", "feed_url": FEED, "entry_id": ENTRY, "tags_text": "cool"},
            headers={"X-Requested-With": "lectio-ajax"},
        )
    assert r.json()["autofetch_pending"] is True
