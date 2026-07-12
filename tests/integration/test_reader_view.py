"""Integration tests for the e-ink reader view (GET /read) and its helpers.

Follows the save-article test pattern: mount just the route on a bare app and
monkeypatch the service layer so the orchestration (backlog selection, prev/next
computation, mark-read, content resolution) is exercised without a DB."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

import main


def _rec(n: int, *, read: bool = False) -> dict:
    return {
        "feed_url": f"feed{n}",
        "id": f"e{n}",
        "title": f"Title {n}",
        "link": f"https://example.com/{n}",
        "read": read,
        "feed_title": "Feed",
    }


def _build_app():
    app = FastAPI()
    app.add_api_route("/read", main.reader_view, methods=["GET"])
    return app


def _patch(monkeypatch, *, backlog, article="<p>BODY</p>"):
    marks: list[tuple] = []
    backlog_kwargs: dict = {}

    def fake_backlog(**kwargs):
        backlog_kwargs.clear()
        backlog_kwargs.update(kwargs)
        return backlog

    def fake_article(feed_url, entry_id, link):
        return f"<p>BODY {entry_id}</p>" if article is None else article

    def fake_mark(feed_url, entry_id, title, link, feed_title):
        marks.append((feed_url, entry_id))

    monkeypatch.setattr(main, "resolve_reader_backlog", fake_backlog)
    monkeypatch.setattr(main, "resolve_reader_article_html", fake_article)
    monkeypatch.setattr(main, "_mark_entry_read_background", fake_mark)
    return marks, backlog_kwargs


def test_renders_first_entry_with_next_and_no_prev(monkeypatch):
    marks, _ = _patch(monkeypatch, backlog=[_rec(1), _rec(2), _rec(3)])
    with TestClient(_build_app()) as client:
        r = client.get("/read")
    assert r.status_code == 200
    body = r.text
    assert "Title 1" in body
    assert "<p>BODY</p>" in body
    # Next points at entry 2; there is no previous on the first entry.
    assert "entry_id=e2" in body
    assert "data-prev=''" in body
    # First entry was unread → marked read.
    assert marks == [("feed1", "e1")]


def test_prev_and_next_for_middle_entry(monkeypatch):
    _patch(monkeypatch, backlog=[_rec(1), _rec(2), _rec(3)])
    with TestClient(_build_app()) as client:
        r = client.get("/read", params={"feed_url": "feed2", "entry_id": "e2", "star_only": "1"})
    assert r.status_code == 200
    body = r.text
    assert "Title 2" in body
    assert "entry_id=e1" in body  # prev
    assert "entry_id=e3" in body  # next


def test_bare_read_defaults_to_saved_unread_oldest(monkeypatch):
    _, kwargs = _patch(monkeypatch, backlog=[_rec(1)])
    with TestClient(_build_app()) as client:
        r = client.get("/read")
    assert r.status_code == 200
    assert kwargs["star_only"] is True
    assert kwargs["read_filter"] == "unread"
    assert kwargs["sort_by"] == "post"
    assert kwargs["sort_dir"] == "asc"


def test_scoped_read_follows_supplied_filter(monkeypatch):
    _, kwargs = _patch(monkeypatch, backlog=[_rec(1)])
    with TestClient(_build_app()) as client:
        r = client.get(
            "/read",
            params={"star_only": "1", "read_filter": "all", "sort_by": "received", "sort_dir": "desc"},
        )
    assert r.status_code == 200
    # Explicit scope disables the bare-bookmark defaults.
    assert kwargs["read_filter"] == "all"
    assert kwargs["sort_by"] == "received"
    assert kwargs["sort_dir"] == "desc"


def test_empty_backlog_shows_empty_page(monkeypatch):
    marks, _ = _patch(monkeypatch, backlog=[])
    with TestClient(_build_app()) as client:
        r = client.get("/read")
    assert r.status_code == 200
    assert "Nothing to read" in r.text
    assert marks == []


def test_entry_not_in_filtered_list_renders_standalone(monkeypatch):
    # e1 was already read and dropped from the unread backlog; opening it should
    # still render, with "next" pointing at the head of the remaining backlog.
    _patch(monkeypatch, backlog=[_rec(2), _rec(3)])
    monkeypatch.setattr(
        main, "get_entry_detail",
        lambda f, e: {"feed_url": "feed1", "id": "e1", "title": "Gone", "link": "https://example.com/1", "read": True},
    )
    with TestClient(_build_app()) as client:
        r = client.get("/read", params={"feed_url": "feed1", "entry_id": "e1", "star_only": "1", "read_filter": "unread"})
    assert r.status_code == 200
    body = r.text
    assert "Gone" in body
    assert "data-prev=''" in body
    assert "entry_id=e2" in body  # next = list head


def test_already_read_entry_is_not_remarked(monkeypatch):
    marks, _ = _patch(monkeypatch, backlog=[_rec(1, read=True), _rec(2)])
    with TestClient(_build_app()) as client:
        r = client.get("/read", params={"feed_url": "feed1", "entry_id": "e1", "star_only": "1"})
    assert r.status_code == 200
    assert marks == []


# --- resolve_reader_article_html: archived → live → stored fallback chain -----

def test_article_html_prefers_archived_copy(monkeypatch):
    monkeypatch.setattr(main, "_resolve_archived_readability_html", lambda f, e: "<p>ARCHIVED</p>")
    # Live extraction must not be consulted when an archive exists.
    monkeypatch.setattr(main, "fetch_readability_article", lambda url: (_ for _ in ()).throw(AssertionError("should not fetch")))
    out = main.resolve_reader_article_html("feed1", "e1", "https://example.com/1")
    assert "ARCHIVED" in out


def test_article_html_falls_back_to_live_then_stored(monkeypatch):
    monkeypatch.setattr(main, "_resolve_archived_readability_html", lambda f, e: None)
    monkeypatch.setattr(main, "fetch_readability_article", lambda url: ("T", "<p>LIVE</p>"))
    assert "LIVE" in main.resolve_reader_article_html("feed1", "e1", "https://example.com/1")

    def boom(url):
        raise RuntimeError("network down")

    monkeypatch.setattr(main, "fetch_readability_article", boom)
    monkeypatch.setattr(main, "get_entry_detail", lambda f, e: {"content_html": "<p>STORED</p>"})
    assert "STORED" in main.resolve_reader_article_html("feed1", "e1", "https://example.com/1")
