"""Canonical entry links: redirector detection, the star-time rewrite hook,
refresh re-pinning, and archive-HTML recovery for dead redirectors."""
from __future__ import annotations

import pytest

import main
from services import tenancy
from services.link_canonical import is_redirector_link, pick_non_redirector

FEED = "https://example.test/feed"
PROXY = "http://feedproxy.google.com/~r/SomeBlog/~3/AbCdEf/"
REAL = "https://someblog.example/post/42"


def test_redirector_detection():
    assert is_redirector_link(PROXY)
    assert is_redirector_link("https://feeds.feedburner.com/~r/Blog/~3/x/")
    # CNAMEd burner domain via the /~r/ path signature.
    assert is_redirector_link("https://feeds.arstechnica.com/~r/arstechnica/index/~3/x/")
    assert not is_redirector_link(REAL)
    assert not is_redirector_link("")


def test_pick_non_redirector():
    assert pick_non_redirector([PROXY, REAL]) == REAL
    assert pick_non_redirector([REAL, PROXY]) == REAL
    assert pick_non_redirector([PROXY]) == PROXY  # nothing better — keep it
    assert pick_non_redirector([]) == ""


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
        reader.add_feed(FEED, exist_ok=True)
        reader.add_entry({"feed_url": FEED, "id": "e1", "title": "t", "link": PROXY})
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _reader_link() -> str | None:
    with main.get_reader() as reader:
        row = reader._storage.get_db().execute(
            "SELECT link FROM entries WHERE feed = ? AND id = 'e1'", (FEED,)
        ).fetchone()
    return row[0] if row else None


def test_apply_canonical_rewrites_and_records_override(configured):
    assert main._apply_canonical_entry_link(FEED, "e1", PROXY, REAL) is True
    assert _reader_link() == REAL
    with main.get_meta_connection() as conn:
        row = conn.execute("SELECT link FROM entry_link_overrides WHERE entry_id='e1'").fetchone()
    assert row and row[0] == REAL


def test_apply_canonical_ignores_non_redirectors(configured):
    # Ordinary redirects (http→https etc.) must not rewrite anything.
    assert main._apply_canonical_entry_link(FEED, "e1", REAL, REAL + "?x") is False
    # A redirector landing on another redirector is not a canonical URL.
    assert main._apply_canonical_entry_link(FEED, "e1", PROXY, "https://feeds.feedburner.com/~r/X/~3/y/") is False
    assert _reader_link() == PROXY


def test_refresh_repins_reverted_link(configured):
    main._apply_canonical_entry_link(FEED, "e1", PROXY, REAL)
    with main.get_reader() as reader:
        db = reader._storage.get_db()
        db.execute("UPDATE entries SET link = ? WHERE feed = ? AND id = 'e1'", (PROXY, FEED))
        db.commit()
    assert main.feed_refresh_service.reapply_entry_link_overrides([FEED]) == 1
    assert _reader_link() == REAL
    assert main.feed_refresh_service.reapply_entry_link_overrides([FEED]) == 0


def test_canonical_from_archived_html():
    from scripts.backfill_canonical_links import canonical_from_html
    page = '<html><head><link rel="canonical" href="https://someblog.example/post/42"/></head></html>'
    assert canonical_from_html(page) == REAL
    og = '<html><head><meta property="og:url" content="https://someblog.example/post/42"></head></html>'
    assert canonical_from_html(og) == REAL
    # A canonical that is itself a redirector is useless.
    bad = f'<link rel="canonical" href="{PROXY}">'
    assert canonical_from_html(bad) is None
