"""A complete archive with no readability copy -- the recapture mismatch guard
refused a parked/replaced page, or its live fetch failed outright -- used to
fall straight through to a fresh live fetch on both Reader View
(GET /entries/readability) and the e-ink reader (resolve_reader_article_html).
That second live fetch can land on the same untrustworthy or dead page the
archive attempt already saw. Fixed to prefer the entry's own stored
content_html (reader's copy, untouched by any of this) in that specific case,
while an ordinary entry with no archive at all still reaches the live fetch --
that is what recovers a full article from a thin RSS stub, the whole reason
Reader View exists."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import main
import routes.entries
from services import tenancy

FEED = "https://example.test/feed"
ENTRY = "https://example.test/post"
LINK = "https://example.test/post"

# looks_like_a_link_index needs 20+ anchors and >=40% of stripped-tag text
# living inside them -- a retired-blog-post-redirected-to-a-category-page shape.
_LINK_INDEX_HTML = (
    "<div>" + "".join(f'<a href="https://example.test/{i}">Article number {i} about guitars</a> ' for i in range(25)) + "</div>"
)


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
    main.ensure_starred_archive_schema()
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _seed_entry(content_html: str) -> None:
    with main.get_reader() as reader:
        reader.add_feed(FEED, exist_ok=True)
        reader.add_entry(
            {
                "feed_url": FEED,
                "id": ENTRY,
                "link": LINK,
                "title": "Post",
                "content": [{"value": content_html, "type": "text/html"}],
            }
        )


def _seed_complete_archive_with_no_readability() -> None:
    with main.archive_conn() as conn:
        conn.execute(
            "INSERT INTO archived_entry (feed_url, entry_id, status, starred_at) VALUES (?, ?, 'complete', 1.0)",
            (FEED, ENTRY),
        )


def _seed_complete_archive_with_readability(readability_html: str) -> None:
    import zlib

    with main.archive_conn() as conn:
        conn.execute(
            "INSERT INTO archived_entry (feed_url, entry_id, status, starred_at, readability_html_zlib) VALUES (?, ?, 'complete', 1.0, ?)",
            (FEED, ENTRY, zlib.compress(readability_html.encode("utf-8"))),
        )


# --- resolve_reader_article_html (e-ink /read view) --------------------------


def test_kept_entry_with_no_readability_copy_uses_stored_content_not_a_live_fetch(configured, monkeypatch):
    _seed_entry("<p>The real stored article body.</p>")
    _seed_complete_archive_with_no_readability()

    def _boom(*a, **k):
        raise AssertionError("must not attempt a live fetch when stored content is available")

    monkeypatch.setattr(main, "fetch_readability_article", _boom)

    result = main.resolve_reader_article_html(FEED, ENTRY, LINK)
    assert "The real stored article body." in result


def test_unkept_entry_with_no_archive_still_uses_the_live_fetch(configured, monkeypatch):
    """No archive at all (never starred/tagged) is the ordinary case -- must
    still reach the live fetch, which is what upgrades a thin RSS stub into a
    full article. This must not regress."""
    _seed_entry("<p>thin stub</p>")

    monkeypatch.setattr(main, "fetch_readability_article", lambda link: ("Title", "<p>Full live-fetched article.</p>"))

    result = main.resolve_reader_article_html(FEED, ENTRY, LINK)
    assert "Full live-fetched article." in result


def test_a_live_fetch_that_looks_like_a_link_index_falls_through_to_stored_content(configured, monkeypatch):
    """A URL folded into a generic category/hub page (guitarworld.com's
    retired /lessons/<slug> URLs all now 301 to one "Lessons Coverage" page)
    extracts as a list of links, not an article. Even with no archive at all,
    that must not be shown -- fall through the same as a failed fetch."""
    _seed_entry("<p>The real stored article body survives the bad live fetch.</p>")

    monkeypatch.setattr(main, "fetch_readability_article", lambda link: ("Lessons Coverage", _LINK_INDEX_HTML))

    result = main.resolve_reader_article_html(FEED, ENTRY, LINK)
    assert "The real stored article body survives the bad live fetch." in result
    assert "Article number" not in result


def test_kept_entry_with_a_real_readability_copy_is_unaffected(configured, monkeypatch):
    # _archived_copy_is_plausible needs >= 400 chars of text (or 2+ images) to
    # count as a real article rather than a failed extraction -- pad well past it.
    archived_text = "<p>" + "The archived article, plenty of real text right here. " * 10 + "</p>"
    _seed_entry("<p>stored content, should not be used here</p>")
    _seed_complete_archive_with_readability(archived_text)

    def _boom(*a, **k):
        raise AssertionError("must not attempt a live fetch when a plausible archived copy exists")

    monkeypatch.setattr(main, "fetch_readability_article", _boom)

    result = main.resolve_reader_article_html(FEED, ENTRY, LINK)
    assert "The archived article" in result


# --- GET /entries/readability (desktop Reader View button) -------------------


def _client() -> TestClient:
    return TestClient(main.app)


def test_readability_route_prefers_stored_content_over_a_second_live_fetch(configured, monkeypatch):
    monkeypatch.setattr(main, "AUTH_ENABLED", False)
    _seed_entry("<p>The real stored article body, route level.</p>")
    _seed_complete_archive_with_no_readability()

    def _boom(url):
        raise AssertionError("must not build a live readability response when stored content is available")

    # entry_readability now lives in routes.entries and did its own `from main
    # import build_readability_response` at module load, so patching main's
    # copy alone would not reach it -- both need patching.
    monkeypatch.setattr(main, "build_readability_response", _boom)
    monkeypatch.setattr(routes.entries, "build_readability_response", _boom)

    resp = _client().get("/entries/readability", params={"url": LINK, "feed_url": FEED, "entry_id": ENTRY})
    assert resp.status_code == 200
    assert "The real stored article body, route level." in resp.text


def test_readability_route_with_no_archive_still_falls_through_to_live_fetch(configured, monkeypatch):
    monkeypatch.setattr(main, "AUTH_ENABLED", False)
    _seed_entry("<p>thin stub</p>")
    monkeypatch.setattr(main, "build_readability_response", lambda url: main.HTMLResponse("<article>live fetched</article>"))
    monkeypatch.setattr(routes.entries, "build_readability_response", lambda url: main.HTMLResponse("<article>live fetched</article>"))

    resp = _client().get("/entries/readability", params={"url": LINK, "feed_url": FEED, "entry_id": ENTRY})
    assert resp.status_code == 200
    assert "live fetched" in resp.text


def test_build_readability_response_refuses_a_link_index_extraction(monkeypatch):
    """A 200 does not mean the article is still there -- readability
    extracting a category/hub page's link list must show an honest failure,
    not the list."""
    monkeypatch.setattr(main, "fetch_readability_article", lambda url: ("Lessons Coverage", _LINK_INDEX_HTML))

    resp = main.build_readability_response("https://example.test/lessons/some-old-post")
    assert resp.status_code == 200
    body = bytes(resp.body).decode("utf-8")
    assert "Could not extract a readable article" in body
    assert "Article number" not in body
