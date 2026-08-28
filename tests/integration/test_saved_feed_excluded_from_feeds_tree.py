"""The Saved Articles virtual feed (lectio:saved) is a real reader feed
(backs the Saved/Kept view) but must never appear as a subscription in Feeds
mode — not as a row in the tree, not actionable via "All Feeds" or
Uncategorized, and not in either folder's post list.

Found 2026-08-24: root ("All Feeds") widens its feed set to every reader feed
so orphan feeds are reachable from the top of the tree, but that widening
used the raw feed set with no exclusion, so lectio:saved could be selected as
`list_feed_url` from "All Feeds", surfacing its entire saved-articles backlog
as if it were an ordinary feed's unread list.

Found 2026-08-27: Uncategorized's tree row/badge already excluded it (via
`_uncat_display_urls`), but the *entry-fetch* scope for actually browsing
into Uncategorized (`folder_feed_urls_by_id[UNCATEGORIZED_FOLDER_ID]`, and
the standalone `get_folder_feed_urls` resolver used by mark-read/refresh
actions) stayed on the raw inclusive set — kept that way deliberately, per
the comment at the time, "so the Saved sidebar's own Uncategorized grouping
can reach its entries." That reasoning only holds for Saved mode; in Feeds
mode it meant clicking into Uncategorized showed the whole saved-articles
backlog mixed in with the real orphan feeds' posts, while the badge (correctly
counting only the real orphans) read far lower than what the list showed.
Saved mode's own reachability is preserved by extending the existing
star_only-gated root re-inclusion in _home_inner to cover Uncategorized too."""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

import main
from services import saved_articles as saved_articles_service
from services import tenancy

ORPHAN_FEED = "https://example.test/orphan-feed"


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "AUTH_ENABLED", False)
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
        saved_articles_service.ensure_saved_feed(reader)
        reader.add_entry({
            "feed_url": saved_articles_service.SAVED_FEED_URL, "id": "https://x.test/a",
            "link": "https://x.test/a", "title": "A saved article",
        })
        # An unfoldered feed too, so Uncategorized isn't empty (and its own
        # inclusion behavior stays observable alongside root's).
        reader.add_feed(ORPHAN_FEED, exist_ok=True)
        reader.add_entry({
            "feed_url": ORPHAN_FEED, "id": "https://orphan.test/a",
            "link": "https://orphan.test/a", "title": "An orphan post",
        })
    with main.get_meta_connection() as conn:
        # A real "save to read later" always stars the entry too — that's
        # what saving means. star_only's kept-entries filter requires it.
        conn.execute(
            "INSERT INTO saved_entries (feed_url, entry_id) VALUES (?, ?)",
            (saved_articles_service.SAVED_FEED_URL, "https://x.test/a"),
        )
        conn.commit()
    main.invalidate_meta_structure_cache()
    try:
        with main.get_meta_connection() as conn:
            yield main.get_root_folder_id(conn)
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved
        main.invalidate_meta_structure_cache()


def _client() -> TestClient:
    return TestClient(main.app)


def test_root_page_never_shows_saved_feed(configured):
    root_id = configured
    resp = _client().get(f"/?folder_id={root_id}")
    assert resp.status_code == 200
    assert saved_articles_service.SAVED_FEED_URL not in resp.text


def test_get_folder_feed_urls_root_excludes_saved_feed(configured):
    root_id = configured
    with main.get_meta_connection() as conn:
        urls = main.get_folder_feed_urls(conn, root_id)
    assert saved_articles_service.SAVED_FEED_URL not in urls
    assert ORPHAN_FEED in urls


def test_get_folder_feed_urls_uncategorized_excludes_saved_feed(configured):
    with main.get_meta_connection() as conn:
        urls = main.get_folder_feed_urls(conn, main.UNCATEGORIZED_FOLDER_ID)
    assert saved_articles_service.SAVED_FEED_URL not in urls
    assert ORPHAN_FEED in urls


def test_uncategorized_page_never_shows_saved_feed(configured):
    resp = _client().get(f"/?folder_id={main.UNCATEGORIZED_FOLDER_ID}")
    assert resp.status_code == 200
    assert saved_articles_service.SAVED_FEED_URL not in resp.text
    # The real orphan feed's post is still there — this isn't hiding
    # Uncategorized itself, just the virtual saved-articles feed within it.
    assert "An orphan post" in resp.text
    assert "A saved article" not in resp.text


def test_saved_mode_uncategorized_still_reaches_saved_feed(configured):
    """The one legitimate need the old inclusive default served: Saved mode's
    own "Uncategorized" grouping (star_only=1) must still be able to list
    lectio:saved's entries — it's an orphan feed same as any other, and
    belongs in the unfoldered subset of the Saved view same as it belongs at
    Saved's root."""
    resp = _client().get(f"/?folder_id={main.UNCATEGORIZED_FOLDER_ID}&star_only=1")
    assert resp.status_code == 200
    assert "A saved article" in resp.text
