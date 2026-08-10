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
