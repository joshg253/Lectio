"""Manual tags on orphan-archive entries (feed gone from `reader`, only the
offline archive capture survives — see main._build_orphan_entry_detail).

Star already works on these via saved_entries, which is meta-DB and has no
reader dependency. Manual tags didn't, because they piggyback on reader's own
entry_tags — an orphan has no reader resource to attach one to, so tagging
silently no-op'd. orphan_entry_tags gives tags the same independence.
"""
from __future__ import annotations

import pytest

import main
from services import tenancy

ORPHAN_FEED = "https://gone.example/feed.xml"
ORPHAN_ENTRY = "https://gone.example/article-1"


def _reset_reader_pool():
    main.close_thread_db_pools()


@pytest.fixture
def orphan_env(tmp_path):
    """A meta/reader DB pair with NO feed or entry at all — every operation
    against ORPHAN_FEED/ORPHAN_ENTRY must hit the orphan fallback path."""
    saved = tenancy._layout
    _reset_reader_pool()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    main.get_reader()  # initialize reader's own schema (entry_tags etc); adds/touches nothing
    main.invalidate_has_manual_tags_cache()
    main.invalidate_tag_counts_cache()
    try:
        yield
    finally:
        _reset_reader_pool()
        tenancy._layout = saved


@pytest.fixture
def archive_spy(monkeypatch):
    archived: list[tuple[str, str]] = []
    removed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        main.starred_archive_service, "enqueue_archive",
        lambda f, e: archived.append((f, e)),
    )
    monkeypatch.setattr(
        main.starred_archive_service, "enqueue_removal",
        lambda f, e: removed.append((f, e)),
    )
    return archived, removed


def test_reader_has_no_entry_for_the_orphan(orphan_env):
    with main.get_reader() as reader:
        assert reader.get_entry((ORPHAN_FEED, ORPHAN_ENTRY), None) is None


def test_set_manual_tags_on_orphan_persists_and_reports_added(orphan_env, archive_spy):
    archived, _removed = archive_spy
    tags = main.set_manual_tags_for_entry(ORPHAN_FEED, ORPHAN_ENTRY, "pshell")
    assert tags == ["pshell"]
    assert (ORPHAN_FEED, ORPHAN_ENTRY) in archived  # tag is a keep signal


def test_get_manual_tags_reads_back_orphan_tags(orphan_env):
    main.set_manual_tags_for_entry(ORPHAN_FEED, ORPHAN_ENTRY, "pshell powershell")
    assert main.get_manual_tags_for_entry(ORPHAN_FEED, ORPHAN_ENTRY) == ["powershell", "pshell"]


def test_removing_last_orphan_tag_releases_archive_when_unstarred(orphan_env, archive_spy):
    _archived, removed = archive_spy
    main.set_manual_tags_for_entry(ORPHAN_FEED, ORPHAN_ENTRY, "pshell")
    main.set_manual_tags_for_entry(ORPHAN_FEED, ORPHAN_ENTRY, "")
    assert (ORPHAN_FEED, ORPHAN_ENTRY) in removed
    assert main.get_manual_tags_for_entry(ORPHAN_FEED, ORPHAN_ENTRY) == []


def test_starred_orphan_keeps_archive_after_untagging(orphan_env, archive_spy):
    _archived, removed = archive_spy
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id) VALUES (?, ?)",
            (ORPHAN_FEED, ORPHAN_ENTRY),
        )
    main.set_manual_tags_for_entry(ORPHAN_FEED, ORPHAN_ENTRY, "temp")
    main.set_manual_tags_for_entry(ORPHAN_FEED, ORPHAN_ENTRY, "")
    assert (ORPHAN_FEED, ORPHAN_ENTRY) not in removed  # star still keeps it


def test_has_any_manual_tags_sees_orphan_only_tags(orphan_env):
    assert main.has_any_manual_tags() is False
    main.set_manual_tags_for_entry(ORPHAN_FEED, ORPHAN_ENTRY, "pshell")
    main.invalidate_has_manual_tags_cache()
    assert main.has_any_manual_tags() is True


def test_orphan_only_tag_appears_in_autocomplete_vocabulary(orphan_env):
    main.set_manual_tags_for_entry(ORPHAN_FEED, ORPHAN_ENTRY, "pshell")
    main.invalidate_has_manual_tags_cache()
    assert "pshell" in main.get_all_manual_tag_names()


def test_delete_manual_tag_everywhere_covers_orphans(orphan_env, archive_spy):
    _archived, removed = archive_spy
    main.set_manual_tags_for_entry(ORPHAN_FEED, ORPHAN_ENTRY, "pshell")
    count = main.delete_manual_tag_everywhere("pshell")
    assert count == 1
    assert main.get_manual_tags_for_entry(ORPHAN_FEED, ORPHAN_ENTRY) == []
    assert (ORPHAN_FEED, ORPHAN_ENTRY) in removed  # untagged + unstarred -> archive released


def test_rename_manual_tag_everywhere_covers_orphans(orphan_env):
    main.set_manual_tags_for_entry(ORPHAN_FEED, ORPHAN_ENTRY, "pshel")
    count, merged = main.rename_manual_tag_everywhere("pshel", "pshell")
    assert count == 1
    assert merged is False
    assert main.get_manual_tags_for_entry(ORPHAN_FEED, ORPHAN_ENTRY) == ["pshell"]


def test_rename_reports_merge_when_target_tag_already_used_on_an_orphan(orphan_env):
    main.set_manual_tags_for_entry(ORPHAN_FEED, ORPHAN_ENTRY, "pshel")
    main.set_manual_tags_for_entry(ORPHAN_FEED, "https://gone.example/article-2", "pshell")
    _count, merged = main.rename_manual_tag_everywhere("pshel", "pshell")
    assert merged is True


def test_build_orphan_entry_detail_surfaces_real_tags(orphan_env, monkeypatch):
    main.set_manual_tags_for_entry(ORPHAN_FEED, ORPHAN_ENTRY, "pshell")
    monkeypatch.setattr(
        main.starred_archive_service, "get_archived_entry_detail",
        lambda f, e: {
            "title": "Working with PowerShell", "link": "https://gone.example/article-1",
            "content_html": "<p>hi</p>", "feed_title": "Gone", "author": None,
            "published_at": None, "received_at": None,
        },
    )
    monkeypatch.setattr(main.starred_archive_service, "get_entry_asset_map", lambda f, e: {})
    detail = main._build_orphan_entry_detail(ORPHAN_FEED, ORPHAN_ENTRY)
    assert detail is not None
    assert detail["manual_tags"] == ["pshell"]
    assert detail["manual_tags_text"] == "pshell"


def _orphan_detail(monkeypatch):
    monkeypatch.setattr(
        main.starred_archive_service, "get_archived_entry_detail",
        lambda f, e: {
            "title": "t", "link": "https://gone.example/article-1", "content_html": "<p>hi</p>",
            "feed_title": "Gone", "author": None, "published_at": None, "received_at": None,
        },
    )
    monkeypatch.setattr(main.starred_archive_service, "get_entry_asset_map", lambda f, e: {})
    return main._build_orphan_entry_detail(ORPHAN_FEED, ORPHAN_ENTRY)


def test_orphan_with_no_signal_is_not_kept(orphan_env, monkeypatch):
    detail = _orphan_detail(monkeypatch)
    assert detail["kept"] is False
    assert detail["saved"] is False


def test_orphan_with_only_a_tag_is_kept_but_not_saved(orphan_env, monkeypatch):
    main.set_manual_tags_for_entry(ORPHAN_FEED, ORPHAN_ENTRY, "pshell")
    detail = _orphan_detail(monkeypatch)
    assert detail["kept"] is True
    assert detail["saved"] is False


def test_orphan_with_only_a_star_is_kept_and_saved(orphan_env, monkeypatch):
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id) VALUES (?, ?)",
            (ORPHAN_FEED, ORPHAN_ENTRY),
        )
    detail = _orphan_detail(monkeypatch)
    assert detail["kept"] is True
    assert detail["saved"] is True


# Feed Properties on an unsubscribed feed: the modal used to hard-fail with
# "Feed not found" (get_feed_properties bails the instant reader.get_feed()
# returns None) and Suggested Tags' Save button silently did nothing (the
# route 404'd on the same check) — so a batch of orphaned-but-kept entries had
# no way to get their one-click tag suggestion chip back. Both routes now
# treat a feed_url the starred archive still recognizes as editable, distinct
# from a genuinely unknown URL.

def _seed_archive_row(feed_url=ORPHAN_FEED, entry_id=ORPHAN_ENTRY, feed_title="Gone Blog"):
    main.ensure_starred_archive_schema()
    with main.get_starred_archive_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO archived_entry (feed_url, entry_id, status, starred_at, feed_title)"
            " VALUES (?, ?, 'complete', 0, ?)",
            (feed_url, entry_id, feed_title),
        )
        conn.commit()


def test_feed_properties_404s_for_a_url_with_no_archive_at_all(orphan_env):
    props = main.get_feed_properties("https://never-heard-of.example/feed")
    assert props["found"] is False


def test_feed_properties_returns_minimal_response_for_known_orphan(orphan_env):
    _seed_archive_row()
    props = main.get_feed_properties(ORPHAN_FEED)
    assert props["found"] is True
    assert props["is_orphan"] is True
    assert props["real_title"] == "Gone Blog"
    assert props["suggested_tags"] == []


def test_feed_properties_orphan_reflects_already_set_suggested_tags(orphan_env):
    _seed_archive_row()
    main.set_feed_pinned_tags(ORPHAN_FEED, "c++ cpp")
    props = main.get_feed_properties(ORPHAN_FEED)
    assert props["suggested_tags"] == ["c++", "cpp"]


def test_suggested_tags_route_saves_for_a_known_orphan_feed(orphan_env):
    _seed_archive_row()
    resp = main.set_feed_suggested_tags_route(feed_url=ORPHAN_FEED, tags="c++")
    assert resp.status_code == 200
    assert main.get_feed_pinned_tags(ORPHAN_FEED) == ["c++"]


def test_suggested_tags_route_still_404s_for_a_genuinely_unknown_url(orphan_env):
    resp = main.set_feed_suggested_tags_route(
        feed_url="https://never-heard-of.example/feed", tags="c++"
    )
    assert resp.status_code == 404
