"""An Instapaper import reports how much of it belongs to feeds already
subscribed to, instead of letting it settle silently into Uncategorized.

That silence is how a 4,000-article unfiled backlog gets built without anyone
noticing. The import deliberately *reports* rather than files: "exactly one
candidate feed" is not the same as a trustworthy one — guitarplayer.com's only
candidate was a scraped single-article stub that would have swallowed 303
articles — so filing stays behind the per-host review, where the evidence is
visible and nothing is pre-checked.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

import main
from services import tenancy
from services.saved_articles import SAVED_FEED_URL

REAL_FEED = "https://blog.example.test/feed"

# Two bookmarks from a host a subscribed feed already covers, one from a host
# nothing covers.
CSV = (
    "URL,Title,Selection,Folder,Timestamp\n"
    "https://blog.example.test/post-a,Post A,,Unread,1600000000\n"
    "https://blog.example.test/post-b,Post B,,Unread,1600000001\n"
    "https://nowhere.test/orphan,Orphan,,Unread,1600000002\n"
)


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
    monkeypatch.setattr(
        main.starred_archive_service, "enqueue_archive", lambda feed_url, entry_id: None
    )
    # A well-stocked subscribed feed on blog.example.test: enough supporting
    # entries to clear MIN_SUPPORT, so its host matches confidently.
    with main.get_reader() as reader:
        reader.add_feed(REAL_FEED, allow_invalid_url=True, exist_ok=True)
        reader.disable_feed_updates(REAL_FEED)
        for i in range(8):
            reader.add_entry({
                "feed_url": REAL_FEED,
                "id": f"existing-{i}",
                "link": f"https://blog.example.test/existing-{i}",
                "title": f"Existing {i}",
            })
    try:
        yield
    finally:
        main.user_store = saved_store
        main.close_thread_db_pools()
        tenancy._layout = saved


def _import(csv: str = CSV) -> dict:
    return main._import_instapaper_for_current_user(csv.encode())


def test_summary_counts_what_could_be_filed(configured):
    summary = _import()
    assert summary["imported"] == 3
    # The two blog.example.test articles match the subscribed feed; the
    # nowhere.test one has no candidate at all.
    assert summary["filable"] == 2
    assert summary["filable_hosts"] == 1


def test_nothing_is_actually_filed(configured):
    """The import must not move anything — filing stays behind the per-host
    review. Every bookmark stays in the saved feed."""
    _import()
    with main.get_reader() as reader:
        db = reader._storage.get_db()
        feeds = dict(db.execute(
            "SELECT feed, COUNT(*) FROM entries GROUP BY feed"
        ).fetchall())
    assert feeds[SAVED_FEED_URL] == 3
    assert feeds[REAL_FEED] == 8  # untouched — no imports moved in


def test_an_import_with_no_matches_reports_zero(configured):
    summary = _import(
        "URL,Title,Selection,Folder,Timestamp\n"
        "https://unknown.test/a,A,,Unread,1600000000\n"
    )
    assert summary["imported"] == 1
    assert summary["filable"] == 0
    assert summary["filable_hosts"] == 0


def test_matching_only_considers_the_newly_imported_articles(configured):
    """The count describes *this* import, not the whole backlog — otherwise a
    small import would report the entire pile and read as a bug."""
    # Pre-existing unfiled save on the matching host, from an earlier session.
    with main.get_reader() as reader:
        main.saved_articles_service.ensure_saved_feed(reader)
        reader.add_entry({
            "feed_url": SAVED_FEED_URL,
            "id": "https://blog.example.test/older",
            "link": "https://blog.example.test/older",
            "title": "Older",
        })
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO saved_entries (feed_url, entry_id) VALUES (?, ?)",
            (SAVED_FEED_URL, "https://blog.example.test/older"),
        )
        conn.commit()

    summary = _import()
    assert summary["filable"] == 2  # the 2 imported, not 3


def test_a_matcher_failure_never_fails_the_import(configured, monkeypatch):
    """The hint is a reporting extra bolted onto a destructive-ish operation
    that has already committed; it must not turn a successful import into an
    error."""
    monkeypatch.setattr(
        main, "_current_autofile_plan",
        lambda restrict_to=None: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    summary = _import()
    assert summary["imported"] == 3
    assert summary["filable"] == 0
