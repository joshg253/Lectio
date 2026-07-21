"""_filter_star_keys_by_search narrows kept (starred/tagged) keys in SQL.

The Saved view's branch runs ahead of the `elif search_terms` fast path, so
before this existed a search there hydrated every kept key via reader.get_entry
and filtered in Python — measured at ~19s per search on a real library, which
reads as a search box that does nothing. The SQL predicate has to mirror the
Python haystack field for field, or the speed-up would silently change results.
"""
from __future__ import annotations

import sqlite3

import pytest

import main
from services import tenancy


@pytest.fixture
def tenant(tmp_path):
    saved = tenancy._layout
    main.close_thread_db_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    try:
        yield tmp_path
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


FEED = "https://blog.test/rss"

# (id, title, link, author, summary)
ROWS = [
    ("e-title",   "Making Cold Brew Coffee", "https://blog.test/a", "",       ""),
    ("e-summary", "Untitled",                "https://blog.test/b", "",       "notes on COFFEE beans"),
    ("e-link",    "Untitled",                "https://blog.test/coffee-tips", "", ""),
    ("e-author",  "Untitled",                "https://blog.test/d", "Jo Coffee", ""),
    ("e-none",    "Bread baking",            "https://blog.test/e", "",       "sourdough starter"),
    ("e-pct",     "100% arabica",            "https://blog.test/f", "",       ""),
    ("e-us",      "cold_brew rig",           "https://blog.test/g", "",       ""),
]


def _seed(tmp_path):
    conn = sqlite3.connect(str(tenancy.reader_db_path()))
    conn.execute("CREATE TABLE entries (id TEXT, feed TEXT, title TEXT, link TEXT,"
                 " author TEXT, summary TEXT, content TEXT, read INTEGER)")
    conn.execute("CREATE TABLE feeds (url TEXT, title TEXT, user_title TEXT)")
    conn.execute("INSERT INTO feeds VALUES (?,?,?)", (FEED, "Kitchen Journal", None))
    conn.executemany(
        "INSERT INTO entries (id, feed, title, link, author, summary, content, read)"
        " VALUES (?,?,?,?,?,?,NULL,0)",
        [(i, FEED, t, ln, a, s) for i, t, ln, a, s in ROWS],
    )
    conn.commit()
    conn.close()
    return {(FEED, r[0]) for r in ROWS}


def _ids(result):
    return {eid for _feed, eid in result}


def test_matches_every_haystack_field(tenant):
    keys = _seed(tenant)
    got = main._filter_star_keys_by_search(keys, ["coffee"])
    # title, summary, link and author all count — same fields the Python
    # haystack concatenates.
    assert _ids(got) == {"e-title", "e-summary", "e-link", "e-author"}


def test_article_body_is_searchable(tenant):
    """A read-later archive is worth searching by its text, not just its
    titles — the metadata-only haystack returned nothing for a phrase that was
    plainly in the article."""
    keys = _seed(tenant)
    conn = sqlite3.connect(str(tenancy.reader_db_path()))
    conn.execute(
        "INSERT INTO entries (id, feed, title, link, author, summary, content, read)"
        " VALUES (?,?,?,?,?,?,?,0)",
        ("e-body", FEED, "Untitled", "https://blog.test/h", "", "",
         '[{"value": "<p>a long digression about espresso machines</p>"}]'),
    )
    conn.commit()
    conn.close()
    keys = keys | {(FEED, "e-body")}
    assert _ids(main._filter_star_keys_by_search(keys, ["espresso"])) == {"e-body"}


def test_feed_title_is_part_of_the_haystack(tenant):
    keys = _seed(tenant)
    got = main._filter_star_keys_by_search(keys, ["kitchen"])
    assert _ids(got) == {r[0] for r in ROWS}  # every entry's feed is "Kitchen Journal"


def test_terms_are_anded_not_ored(tenant):
    keys = _seed(tenant)
    assert _ids(main._filter_star_keys_by_search(keys, ["coffee", "brew"])) == {"e-title"}
    assert _ids(main._filter_star_keys_by_search(keys, ["coffee", "zzz"])) == set()


def test_search_is_case_insensitive(tenant):
    keys = _seed(tenant)
    # "COFFEE beans" in a summary must match the lowercased term.
    assert "e-summary" in _ids(main._filter_star_keys_by_search(keys, ["coffee"]))


@pytest.mark.parametrize("term,expected", [
    ("100%", {"e-pct"}),        # % is a LIKE wildcard — must be treated literally
    ("cold_brew", {"e-us"}),    # so is _
])
def test_like_wildcards_in_a_term_are_literal(tenant, term, expected):
    keys = _seed(tenant)
    assert _ids(main._filter_star_keys_by_search(keys, [term])) == expected


def test_empty_terms_pass_the_set_through(tenant):
    keys = _seed(tenant)
    assert main._filter_star_keys_by_search(keys, []) == keys


def test_missing_entries_table_falls_back_to_python(tenant):
    """None tells the caller to keep the full set and post-filter, rather than
    silently returning an empty result and showing no posts."""
    assert main._filter_star_keys_by_search({(FEED, "x")}, ["coffee"]) is None


def test_chunks_past_the_sqlite_variable_limit(tenant):
    """450 keys per chunk (2 bound vars each); a larger set must still work."""
    _seed(tenant)
    conn = sqlite3.connect(str(tenancy.reader_db_path()))
    extra = [(f"bulk-{i}", FEED, "Coffee bulk", "https://blog.test/x", "", "")
             for i in range(1200)]
    conn.executemany(
        "INSERT INTO entries (id, feed, title, link, author, summary, content, read)"
        " VALUES (?,?,?,?,?,?,NULL,0)", extra)
    conn.commit()
    conn.close()
    keys = {(FEED, r[0]) for r in ROWS} | {(FEED, e[0]) for e in extra}
    got = main._filter_star_keys_by_search(keys, ["coffee"])
    assert len(got) == 4 + 1200
