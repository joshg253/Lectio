"""POST /saved/refetch-scope: the date_choice JSON field, threaded through to
each article in the batch the same way /articles/refresh-content threads it
for a single one -- see tests/integration/test_refetch_content_route.py.

Validated against the same whitelist and forwarded via the job dict
(_refetch_begin) rather than a request-scoped variable, since the actual
batch runs on a background thread. The background thread itself is not
exercised here -- _refetch_begin runs synchronously before it's spawned, so
inspecting the job dict right after the route returns is enough to prove the
value made it through, without needing real reader/kept-entry fixtures.
_run_in_user_context is stubbed (main's own function, not stdlib threading)
so the spawned thread has nothing real to do.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main


@pytest.fixture(autouse=True)
def _isolated_refetch_jobs(monkeypatch):
    monkeypatch.setattr(main, "_refetch_jobs", {})
    monkeypatch.setattr(main, "_scope_refetchable", lambda folder_id, list_feed_url: [
        ("https://example.test/feed", "https://example.test/post", "https://example.test/post"),
    ])
    monkeypatch.setattr(main.refetch_batch, "estimate_seconds", lambda rows: 5)
    monkeypatch.setattr(main, "_run_in_user_context", lambda uid, fn, *a, **kw: None)


def _build_app():
    app = FastAPI()
    app.post("/saved/refetch-scope")(main.start_refetch_scope)
    return app


def _post(**body):
    app = _build_app()
    with TestClient(app) as client:
        return client.post("/saved/refetch-scope", json={"folder_id": 1, **body})


def test_valid_choices_pass_through():
    for choice in ("now", "original", "pub"):
        main._refetch_jobs.clear()  # each iteration starts fresh, not queued behind the last
        r = _post(date_choice=choice)
        assert r.status_code == 200
        job = main._refetch_job_state()
        assert job is not None and job["date_choice"] == choice


def test_blank_date_choice_becomes_none():
    r = _post()  # date_choice omitted entirely
    assert r.status_code == 200
    job = main._refetch_job_state()
    assert job is not None and job["date_choice"] is None


def test_unrecognized_date_choice_becomes_none():
    r = _post(date_choice="whenever")
    assert r.status_code == 200
    job = main._refetch_job_state()
    assert job is not None and job["date_choice"] is None


def test_queued_scope_carries_its_own_date_choice():
    """A second scope queued behind a running batch must remember its own
    date_choice for when it actually starts, not the running batch's."""
    job = main._refetch_job_state(create=True)
    job["running"] = True
    job["queue"] = []
    r = _post(folder_id=2, date_choice="pub")
    assert r.status_code == 200
    data = r.json()
    assert data["queued"] is True
    assert job["queue"][0]["date_choice"] == "pub"
