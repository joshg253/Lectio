"""Feeds-view search resolves in SQL rather than through reader's FTS index.

`search_entries` builds a highlighted snippet per result, which measured at ~95%
of a 10-20s search on the live library — so both search surfaces now narrow to
matching keys in SQL and hydrate only the survivors. These tests pin the
predicate: which fields are searched, how multiple terms combine, feed scoping,
read filtering, and that user input can't act as a LIKE pattern."""
from __future__ import annotations

import pytest

import main
from services import tenancy

FEED_A = "https://a.test/feed"
FEED_B = "https://b.test/feed"


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
    with main.get_reader() as reader:
        for url, title in ((FEED_A, "Alpha Feed"), (FEED_B, "Beta Feed")):
            reader.add_feed(url, allow_invalid_url=True, exist_ok=True)
            reader.disable_feed_updates(url)
            reader.set_feed_user_title(url, title)
        reader.add_entry({
            "feed_url": FEED_A, "id": "a1", "title": "Making espresso at home",
            "link": "https://a.test/espresso", "summary": "beans and grinders",
        })
        reader.add_entry({
            "feed_url": FEED_A, "id": "a2", "title": "Unrelated post",
            "link": "https://a.test/other",
            "content": [{"value": "<p>a phrase buried in the article body: quokka</p>"}],
        })
        reader.add_entry({
            "feed_url": FEED_B, "id": "b1", "title": "Espresso machines reviewed",
            "link": "https://b.test/machines",
        })
        reader.add_entry({
            "feed_url": FEED_B, "id": "b2", "title": "100% pure",
            "link": "https://b.test/a_b",
        })
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _search(terms, feeds=None, read=None, limit=100):
    return main._search_entry_keys_in_sql(terms, feeds or {FEED_A, FEED_B}, read, limit)


def test_matches_the_title(configured):
    assert set(_search(["espresso"])) == {(FEED_A, "a1"), (FEED_B, "b1")}


def test_matches_the_stored_article_body(configured):
    """The point of searching content: a phrase from inside an article, not
    only its metadata."""
    assert set(_search(["quokka"])) == {(FEED_A, "a2")}


def test_matches_the_feed_title(configured):
    assert set(_search(["beta"])) == {(FEED_B, "b1"), (FEED_B, "b2")}


def test_matches_the_summary_and_link(configured):
    assert set(_search(["grinders"])) == {(FEED_A, "a1")}
    assert set(_search(["machines"])) == {(FEED_B, "b1")}


def test_multiple_terms_are_anded(configured):
    assert set(_search(["espresso", "home"])) == {(FEED_A, "a1")}
    assert _search(["espresso", "quokka"]) == []


def test_search_is_case_insensitive(configured):
    assert set(_search(["ESPRESSO"])) == {(FEED_A, "a1"), (FEED_B, "b1")}


def test_scopes_to_the_selected_feeds(configured):
    assert set(_search(["espresso"], feeds={FEED_B})) == {(FEED_B, "b1")}


def test_read_filter_narrows(configured):
    with main.get_reader() as reader:
        reader.set_entry_read((FEED_A, "a1"), True)
    assert set(_search(["espresso"], read=False)) == {(FEED_B, "b1")}
    assert set(_search(["espresso"], read=True)) == {(FEED_A, "a1")}


def test_limit_is_applied(configured):
    assert len(_search(["espresso"], limit=1)) == 1


@pytest.mark.parametrize("term", ["100%", "a_b"])
def test_like_wildcards_in_user_input_are_literals(configured, term):
    """`%` and `_` are characters the user typed, not patterns — unescaped,
    "100%" would match every entry and "a_b" would match any a?b."""
    assert set(_search([term])) == {(FEED_B, "b2")}


def test_an_underscore_does_not_act_as_a_single_char_wildcard(configured):
    """The companion to the above: "a_b" must not match "axb"."""
    with main.get_reader() as reader:
        reader.add_entry({
            "feed_url": FEED_B, "id": "b3", "title": "decoy", "link": "https://b.test/axb",
        })
    assert set(_search(["a_b"])) == {(FEED_B, "b2")}


def test_no_terms_returns_none(configured):
    """None means 'not handled' so the caller keeps its own path."""
    assert _search([]) is None
