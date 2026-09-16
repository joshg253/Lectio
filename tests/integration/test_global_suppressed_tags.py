"""Global ignored suggested-tags list (Settings -> Tags): a tag value that
should never render as a suggestion chip on any feed, distinct from the
existing per-(feed, tag) dismissal (suppressed_feed_tags). Editable via
/tags/global-suppressed/{add,remove} and read back via GET /tags/global-suppressed.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
from services import tenancy

FEED = "https://example.test/feed"
ENTRY = "e1"


@pytest.fixture
def configured(tmp_path):
    saved = tenancy._layout
    main.close_thread_db_pools()
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
        main.close_thread_db_pools()
        tenancy._layout = saved


def _client():
    app = FastAPI()
    app.get("/tags/global-suppressed")(main.list_globally_suppressed_tags_route)
    app.post("/tags/global-suppressed/add")(main.add_globally_suppressed_tag_route)
    app.post("/tags/global-suppressed/remove")(main.remove_globally_suppressed_tag_route)
    return TestClient(app)


def test_add_list_remove_round_trip(configured):
    client = _client()

    r = client.get("/tags/global-suppressed")
    assert r.json() == {"ok": True, "tags": []}

    r = client.post("/tags/global-suppressed/add", data={"tag": "Comments"})
    assert r.json() == {"ok": True, "tags": ["Comments"]}

    r = client.get("/tags/global-suppressed")
    assert r.json() == {"ok": True, "tags": ["Comments"]}

    r = client.post("/tags/global-suppressed/remove", data={"tag": "comments"})
    assert r.json() == {"ok": True, "tags": []}


def test_add_rejects_a_blank_tag(configured):
    client = _client()
    r = client.post("/tags/global-suppressed/add", data={"tag": "   "})
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_added_tag_disappears_from_suggestions_on_any_feed(configured):
    other_feed = "https://example.test/other"
    with main.get_reader() as reader:
        reader.add_feed(FEED, exist_ok=True)
        reader.add_entry({"feed_url": FEED, "id": ENTRY, "link": f"{FEED}#{ENTRY}", "title": "t"})
        reader.add_feed(other_feed, exist_ok=True)
        reader.add_entry({"feed_url": other_feed, "id": "e2", "link": f"{other_feed}#e2", "title": "t2"})
    main.feed_tag_service.record_entry_tags(FEED, [(ENTRY, ["Comments", "Nintendo Switch"])])
    main.feed_tag_service.record_entry_tags(other_feed, [("e2", ["Comments", "Popular Deals"])])

    assert main.get_feed_tag_suggestions(FEED, ENTRY) == ["Comments", "Nintendo Switch"]
    assert main.get_feed_tag_suggestions(other_feed, "e2") == ["Comments", "Popular Deals"]

    client = _client()
    client.post("/tags/global-suppressed/add", data={"tag": "Comments"})

    assert main.get_feed_tag_suggestions(FEED, ENTRY) == ["Nintendo Switch"]
    assert main.get_feed_tag_suggestions(other_feed, "e2") == ["Popular Deals"]


def test_removed_tag_reappears_in_suggestions(configured):
    with main.get_reader() as reader:
        reader.add_feed(FEED, exist_ok=True)
        reader.add_entry({"feed_url": FEED, "id": ENTRY, "link": f"{FEED}#{ENTRY}", "title": "t"})
    main.feed_tag_service.record_entry_tags(FEED, [(ENTRY, ["Comments"])])

    client = _client()
    client.post("/tags/global-suppressed/add", data={"tag": "Comments"})
    assert main.get_feed_tag_suggestions(FEED, ENTRY) == []

    client.post("/tags/global-suppressed/remove", data={"tag": "Comments"})
    assert main.get_feed_tag_suggestions(FEED, ENTRY) == ["Comments"]


def test_per_feed_dismissal_is_unaffected_by_the_global_list(configured):
    """The two axes are independent: dismissing a tag globally must not touch
    (or be touched by) a feed's own suppressed_feed_tags rows."""
    with main.get_reader() as reader:
        reader.add_feed(FEED, exist_ok=True)
        reader.add_entry({"feed_url": FEED, "id": ENTRY, "link": f"{FEED}#{ENTRY}", "title": "t"})
    main.feed_tag_service.record_entry_tags(FEED, [(ENTRY, ["Comments", "Popular Deals"])])
    main.feed_tag_service.set_tag_suppressed(FEED, "Popular Deals", True)

    client = _client()
    client.post("/tags/global-suppressed/add", data={"tag": "Comments"})

    assert main.get_feed_tag_suggestions(FEED, ENTRY) == []
    assert main.feed_tag_service.suppressed_tag_list(FEED) == ["Popular Deals"]
