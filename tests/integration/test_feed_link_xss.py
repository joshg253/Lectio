"""A feed-supplied javascript: link must never reach a rendered href.

Confirmed exploitable before the fix: an entry whose `link` was
`javascript:alert(...)` rendered as four live `href="javascript:..."`
attributes (entry title, source buttons) plus data-source-url/data-post-link.
A malicious — or compromised — feed could therefore run script in Lectio's
origin the moment the user clicked the article, with the session cookie and
CSRF token in scope. CodeQL flagged only the JS half (js/xss-through-dom on
static/js/app.js); the server-rendered hrefs were the worse half.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

import main
from services import tenancy

FEED = "https://evil.test/feed"
BAD = "javascript:alert(document.domain)"
GOOD = "https://good.test/article"


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
    with main.get_reader() as reader:
        reader.add_feed(FEED, exist_ok=True)
        reader.add_entry({"feed_url": FEED, "id": "bad", "title": "Malicious", "link": BAD})
        reader.add_entry({"feed_url": FEED, "id": "good", "title": "Benign", "link": GOOD})
    main.invalidate_meta_structure_cache()
    main.invalidate_unread_counts_cache()
    try:
        yield
    finally:
        main.user_store = saved_store
        main.close_thread_db_pools()
        tenancy._layout = saved


def _client() -> TestClient:
    return TestClient(main.app)


def test_javascript_link_never_rendered_in_entry_pane(configured):
    body = _client().get(f"/?folder_id=1&feed_url={FEED}&entry_id=bad").text
    # The feed's payload must appear nowhere — not as an href, not in the
    # data-* attributes the JS reads back out of the DOM.
    assert BAD not in body
    assert "javascript:alert" not in body
    assert 'data-source-url="javascript:' not in body
    assert 'data-post-link="javascript:' not in body
    # NB: the page legitimately contains one `href="javascript:…"` — the
    # drag-to-bookmarks-bar bookmarklet in Settings, which is our own static
    # template code, not feed input. So don't assert on that substring alone.


def test_javascript_link_never_rendered_in_post_list(configured):
    body = _client().get(f"/?folder_id=1&list_feed_url={FEED}").text
    assert "javascript:alert" not in body


def test_entry_detail_empties_unsafe_link(configured):
    detail = main.get_entry_detail(FEED, "bad")
    assert detail["link"] == ""


def test_benign_links_still_work(configured):
    """The guard must not break normal articles."""
    detail = main.get_entry_detail(FEED, "good")
    assert detail["link"] == GOOD
    body = _client().get(f"/?folder_id=1&feed_url={FEED}&entry_id=good").text
    assert GOOD in body


def test_post_list_keeps_benign_link(configured):
    posts = main.list_entries_for_feeds({FEED}, read_filter="all")
    by_id = {p["id"]: p for p in posts}
    assert by_id["good"]["link"] == GOOD
    assert by_id["bad"]["link"] == ""
