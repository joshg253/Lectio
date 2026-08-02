"""/rules/tag-vocabulary — the suggestion source for the rule form's
comma-separated tag_filter field.

The route's job is to hand back tags in the form the *spec parser* will see,
scoped the way the rule will be scoped. Both halves are load-bearing: an
un-normalized suggestion completes to a token that cannot match, and an
un-scoped one offers a vocabulary from feeds the rule never touches.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
from services import tenancy

FEED = "https://firehose.example.test/feed"
OTHER = "https://quiet.example.test/feed"


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


@pytest.fixture
def client():
    app = FastAPI()
    app.get("/rules/tag-vocabulary")(main.rules_tag_vocabulary_route)
    with TestClient(app) as c:
        yield c


def _seed() -> None:
    main.feed_tag_service.record_entry_tags(FEED, [
        ("e1", ["Steam Deck", "Indie Game"]),
        ("e2", ["Steam Deck"]),
        ("e3", ["steam deck", "Xbox Series X/S"]),
    ])
    main.feed_tag_service.record_entry_tags(OTHER, [("e1", ["Knitting"])])


def test_tags_come_back_normalized_and_by_frequency(configured, client):
    """Stored 'Steam Deck' must arrive as 'steam-deck': that is what
    parse_tag_filter_spec produces from what the user types, so completing a
    suggestion has to produce a token that matches by construction."""
    _seed()
    r = client.get("/rules/tag-vocabulary", params={"scope": "feed", "scope_id": FEED})
    assert r.status_code == 200
    tags = r.json()["tags"]
    assert tags[0] == {"tag": "steam-deck", "count": 3}
    assert {t["tag"] for t in tags} >= {"indie-game", "steam-deck"}
    assert all(t["tag"] == t["tag"].lower() and " " not in t["tag"] for t in tags)


def test_the_vocabulary_is_scoped_like_the_rule(configured, client):
    _seed()
    r = client.get("/rules/tag-vocabulary", params={"scope": "feed", "scope_id": OTHER})
    assert [t["tag"] for t in r.json()["tags"]] == ["knitting"]


def test_global_scope_spans_every_feed(configured, client):
    _seed()
    r = client.get("/rules/tag-vocabulary", params={"scope": "global", "scope_id": ""})
    assert "knitting" in {t["tag"] for t in r.json()["tags"]}
    assert "steam-deck" in {t["tag"] for t in r.json()["tags"]}


def test_casing_variants_merge_into_one_suggestion(configured, client):
    """'Steam Deck' x2 + 'steam deck' x1 is one tag on three entries, not two
    tags splitting the count — otherwise the number that decides whether a
    filter is worth writing is wrong."""
    _seed()
    r = client.get("/rules/tag-vocabulary", params={"scope": "feed", "scope_id": FEED})
    steam = [t for t in r.json()["tags"] if t["tag"] == "steam-deck"]
    assert steam == [{"tag": "steam-deck", "count": 3}]


def test_an_empty_feeds_scope_suggests_nothing(configured, client):
    """A 'feeds' rule with no resolvable picks must not fall through to the
    whole library — that would suggest tags from feeds it cannot act on."""
    _seed()
    r = client.get("/rules/tag-vocabulary", params={"scope": "feeds", "scope_id": ""})
    assert r.json()["tags"] == []
