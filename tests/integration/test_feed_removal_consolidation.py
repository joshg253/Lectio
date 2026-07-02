"""Integration tests for the consolidated feed-removal helper (purge_orphaned_feed)
and the WebSub push indicator (get_push_active_feed_urls).

Tests pin the behavior that was previously ad-hoc across 5 copy-pasted call sites
so future drift is caught immediately.

Coverage matrix
---------------
- purge_orphaned_feed: archive_pending, rescue_to, DA path, scraped path, plain path
- remove_feed_from_folder (Site A): basic sanity, still delegates
- /feeds/unsubscribe (Site B): now calls websub unsubscribe + DA/scraped paths
- delete_folder (Site C): force-archives + websub-unsubscribes orphaned feeds
- deduplicate_feeds (Site D): websub-unsubscribes the removed URL
- get_push_active_feed_urls (Part B): verified vs pending/no-hub
"""
from __future__ import annotations

import datetime as dt
import time
from unittest.mock import MagicMock, patch

import pytest

import main
from services import tenancy

FEED = "https://example.test/feed"
FEED2 = "https://example.test/feed/"  # slash variant for dedup tests
BASE = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _reset_pools() -> None:
    main._reader_thread_local.pool = None
    main._meta_conn_local.pool = None
    main._websub_conn_local.pool = None


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Minimal single-user env: reader + meta DB in tmp_path."""
    saved_layout = tenancy._layout
    _reset_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    monkeypatch.setattr(main, "WEBSUB_DB_PATH", tmp_path / "lectio_websub.sqlite")
    main.ensure_meta_schema()
    main.ensure_websub_schema()
    try:
        yield tmp_path
    finally:
        _reset_pools()
        tenancy._layout = saved_layout


def _add_feed_to_folder(feed_url: str, folder_id: int) -> None:
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)",
            (folder_id, feed_url),
        )
    with main.get_reader() as reader:
        reader.add_feed(feed_url, allow_invalid_url=True, exist_ok=True)


def _root_folder_id() -> int:
    with main.get_meta_connection() as conn:
        return main.get_root_folder_id(conn)


def _make_child_folder(name: str) -> int:
    root = _root_folder_id()
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO folders (name, parent_id) VALUES (?, ?)",
            (name, root),
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


# ---------------------------------------------------------------------------
# purge_orphaned_feed — unit-level (mocked services)
# ---------------------------------------------------------------------------

class TestPurgeOrphanedFeed:
    """purge_orphaned_feed runs the correct step sequence depending on flags."""

    def test_plain_feed_calls_reader_delete(self, env, monkeypatch):
        _add_feed_to_folder(FEED, _root_folder_id())
        archive_mock = MagicMock(return_value=0)
        ws_mock = MagicMock()
        monkeypatch.setattr(main.starred_archive_service, "force_archive_pending_for_feed", archive_mock)
        monkeypatch.setattr(main, "websub_service", ws_mock)
        with main.get_reader() as reader:
            with main.get_meta_connection() as conn:
                main.purge_orphaned_feed(reader, conn, FEED, archive_pending=True)
        archive_mock.assert_called_once_with(FEED)
        ws_mock.unsubscribe.assert_called_once_with(FEED, tenancy.DEFAULT_USER_ID)
        # Feed should be gone from reader.
        with main.get_reader() as reader:
            assert not any(True for _ in reader.get_feeds())

    def test_archive_pending_false_skips_force_archive(self, env, monkeypatch):
        _add_feed_to_folder(FEED, _root_folder_id())
        archive_mock = MagicMock(return_value=0)
        monkeypatch.setattr(main.starred_archive_service, "force_archive_pending_for_feed", archive_mock)
        monkeypatch.setattr(main, "websub_service", None)
        with main.get_reader() as reader:
            with main.get_meta_connection() as conn:
                main.purge_orphaned_feed(reader, conn, FEED, archive_pending=False)
        archive_mock.assert_not_called()

    def test_rescue_to_is_forwarded(self, env, monkeypatch):
        _add_feed_to_folder(FEED, _root_folder_id())
        _add_feed_to_folder(FEED2, _root_folder_id())
        rescue_mock = MagicMock(return_value=0)
        monkeypatch.setattr(main, "_rescue_unread_entries", rescue_mock)
        monkeypatch.setattr(main.starred_archive_service, "force_archive_pending_for_feed", MagicMock(return_value=0))
        monkeypatch.setattr(main, "websub_service", None)
        with main.get_reader() as reader:
            with main.get_meta_connection() as conn:
                main.purge_orphaned_feed(reader, conn, FEED, archive_pending=False, rescue_to=FEED2)
        rescue_mock.assert_called_once_with(reader, FEED, FEED2)

    def test_rescue_to_returns_rescued_count(self, env, monkeypatch):
        """purge returns the count from _rescue_unread_entries so dedup can total it."""
        _add_feed_to_folder(FEED, _root_folder_id())
        _add_feed_to_folder(FEED2, _root_folder_id())
        monkeypatch.setattr(main, "_rescue_unread_entries", MagicMock(return_value=4))
        monkeypatch.setattr(main.starred_archive_service, "force_archive_pending_for_feed", MagicMock(return_value=0))
        monkeypatch.setattr(main, "websub_service", None)
        with main.get_reader() as reader:
            with main.get_meta_connection() as conn:
                rescued = main.purge_orphaned_feed(reader, conn, FEED, archive_pending=False, rescue_to=FEED2)
        assert rescued == 4

    def test_no_rescue_returns_zero(self, env, monkeypatch):
        """Without rescue_to, purge rescues nothing and returns 0."""
        _add_feed_to_folder(FEED, _root_folder_id())
        monkeypatch.setattr(main.starred_archive_service, "force_archive_pending_for_feed", MagicMock(return_value=0))
        monkeypatch.setattr(main, "websub_service", None)
        with main.get_reader() as reader:
            with main.get_meta_connection() as conn:
                rescued = main.purge_orphaned_feed(reader, conn, FEED, archive_pending=True)
        assert rescued == 0

    def test_websub_none_does_not_raise(self, env, monkeypatch):
        _add_feed_to_folder(FEED, _root_folder_id())
        monkeypatch.setattr(main.starred_archive_service, "force_archive_pending_for_feed", MagicMock(return_value=0))
        monkeypatch.setattr(main, "websub_service", None)
        with main.get_reader() as reader:
            with main.get_meta_connection() as conn:
                # Should not raise even with no websub_service.
                main.purge_orphaned_feed(reader, conn, FEED, archive_pending=True)

    def test_da_feed_dispatches_to_deviantart_service(self, env, monkeypatch):
        da_id = "da-uuid-1"
        da_url = f"file:///fake-da-dir/{da_id}.xml"
        _add_feed_to_folder(da_url, _root_folder_id())
        monkeypatch.setattr(main.starred_archive_service, "force_archive_pending_for_feed", MagicMock(return_value=0))
        monkeypatch.setattr(main, "websub_service", None)
        monkeypatch.setattr(main.deviantart_service, "deviantart_feed_id_from_url", lambda url: da_id if url == da_url else None)
        da_delete = MagicMock()
        monkeypatch.setattr(main.deviantart_service, "delete_deviantart_feed", da_delete)
        with main.get_reader() as reader:
            with main.get_meta_connection() as conn:
                main.purge_orphaned_feed(reader, conn, da_url, archive_pending=True)
        da_delete.assert_called_once()
        assert da_delete.call_args[0][2] == da_id  # third arg is da_id

    def test_scraped_feed_dispatches_to_scraper_service(self, env, monkeypatch):
        sc_id = "sc-uuid-1"
        sc_url = f"file:///fake-sc-dir/{sc_id}.xml"
        _add_feed_to_folder(sc_url, _root_folder_id())
        monkeypatch.setattr(main.starred_archive_service, "force_archive_pending_for_feed", MagicMock(return_value=0))
        monkeypatch.setattr(main, "websub_service", None)
        monkeypatch.setattr(main.deviantart_service, "deviantart_feed_id_from_url", lambda url: None)
        monkeypatch.setattr(main.scraper_service, "scraped_feed_id_from_url", lambda url: sc_id if url == sc_url else None)
        sc_delete = MagicMock()
        monkeypatch.setattr(main.scraper_service, "delete_scraped_feed", sc_delete)
        with main.get_reader() as reader:
            with main.get_meta_connection() as conn:
                main.purge_orphaned_feed(reader, conn, sc_url, archive_pending=True)
        sc_delete.assert_called_once()
        assert sc_delete.call_args[0][2] == sc_id  # third arg is feed_id


# ---------------------------------------------------------------------------
# Site B — /feeds/unsubscribe route
# ---------------------------------------------------------------------------

class TestUnsubscribeRoute:
    """The unsubscribe button must fully clean up: archive, DA/scraped delete, websub."""

    def test_unsubscribe_calls_websub(self, env, monkeypatch):
        fid = _make_child_folder("TestFolder")
        _add_feed_to_folder(FEED, fid)
        ws_mock = MagicMock()
        monkeypatch.setattr(main, "websub_service", ws_mock)
        monkeypatch.setattr(main.starred_archive_service, "force_archive_pending_for_feed", MagicMock(return_value=0))
        # Simulate the route's orphan path: drop the folder_feeds row, confirm the
        # feed is no longer referenced, then purge — same sequence the route runs.
        with main.get_meta_connection() as conn:
            conn.execute(
                "DELETE FROM folder_feeds WHERE folder_id = ? AND feed_url = ?",
                (fid, FEED),
            )
            still_used = conn.execute(
                "SELECT 1 FROM folder_feeds WHERE feed_url = ? LIMIT 1", (FEED,)
            ).fetchone()
        assert not still_used
        with main.get_reader() as reader:
            with main.get_meta_connection() as conn:
                main.purge_orphaned_feed(reader, conn, FEED, archive_pending=True)
        ws_mock.unsubscribe.assert_called_once_with(FEED, tenancy.DEFAULT_USER_ID)

    def test_unsubscribe_via_remove_feed_from_folder_calls_websub(self, env, monkeypatch):
        """remove_feed_from_folder (used by the unsubscribe route helper) calls websub."""
        fid = _make_child_folder("Folder1")
        _add_feed_to_folder(FEED, fid)
        ws_mock = MagicMock()
        monkeypatch.setattr(main, "websub_service", ws_mock)
        monkeypatch.setattr(main.starred_archive_service, "force_archive_pending_for_feed", MagicMock(return_value=0))
        main.remove_feed_from_folder(FEED, fid)
        ws_mock.unsubscribe.assert_called_once_with(FEED, tenancy.DEFAULT_USER_ID)

    def test_unsubscribe_via_remove_feed_calls_da_delete(self, env, monkeypatch):
        """remove_feed_from_folder routes DA feeds through deviantart_service.delete_deviantart_feed."""
        da_id = "da-uuid-2"
        da_url = f"file:///fake-da-dir/{da_id}.xml"
        fid = _make_child_folder("Folder2")
        _add_feed_to_folder(da_url, fid)
        monkeypatch.setattr(main.starred_archive_service, "force_archive_pending_for_feed", MagicMock(return_value=0))
        monkeypatch.setattr(main, "websub_service", None)
        monkeypatch.setattr(main.deviantart_service, "deviantart_feed_id_from_url", lambda url: da_id if url == da_url else None)
        da_delete = MagicMock()
        monkeypatch.setattr(main.deviantart_service, "delete_deviantart_feed", da_delete)
        main.remove_feed_from_folder(da_url, fid)
        da_delete.assert_called_once()

    def test_unsubscribe_via_remove_feed_calls_scraped_delete(self, env, monkeypatch):
        """remove_feed_from_folder routes scraped feeds through scraper_service.delete_scraped_feed."""
        sc_id = "sc-uuid-2"
        sc_url = f"file:///fake-sc-dir/{sc_id}.xml"
        fid = _make_child_folder("Folder3")
        _add_feed_to_folder(sc_url, fid)
        monkeypatch.setattr(main.starred_archive_service, "force_archive_pending_for_feed", MagicMock(return_value=0))
        monkeypatch.setattr(main, "websub_service", None)
        monkeypatch.setattr(main.deviantart_service, "deviantart_feed_id_from_url", lambda url: None)
        monkeypatch.setattr(main.scraper_service, "scraped_feed_id_from_url", lambda url: sc_id if url == sc_url else None)
        sc_delete = MagicMock()
        monkeypatch.setattr(main.scraper_service, "delete_scraped_feed", sc_delete)
        main.remove_feed_from_folder(sc_url, fid)
        sc_delete.assert_called_once()

    def test_feed_in_two_folders_unsubscribe_one_keeps_feed(self, env, monkeypatch):
        """Removing from one folder when still in another must NOT delete the feed."""
        fid1 = _make_child_folder("F1")
        fid2 = _make_child_folder("F2")
        _add_feed_to_folder(FEED, fid1)
        _add_feed_to_folder(FEED, fid2)
        ws_mock = MagicMock()
        monkeypatch.setattr(main, "websub_service", ws_mock)
        monkeypatch.setattr(main.starred_archive_service, "force_archive_pending_for_feed", MagicMock(return_value=0))
        main.remove_feed_from_folder(FEED, fid1)
        ws_mock.unsubscribe.assert_not_called()
        with main.get_reader() as reader:
            feeds = list(reader.get_feeds())
        assert any(str(f.url) == FEED for f in feeds)


# ---------------------------------------------------------------------------
# get_folder_feed_urls — root ("All Feeds") must include uncategorized feeds
# ---------------------------------------------------------------------------

class TestRootFeedResolution:
    def test_root_includes_uncategorized_feeds(self, env):
        """A feed in no folder must be reachable from All Feeds, so mark-read /
        mark-older / refresh on the root operate on it (regression: root actions
        used to skip orphan feeds that the list view still displayed)."""
        foldered = _make_child_folder("Foldered")
        _add_feed_to_folder(FEED, foldered)
        # FEED2 lives in the reader but in no folder → Uncategorized.
        with main.get_reader() as reader:
            reader.add_feed(FEED2, allow_invalid_url=True, exist_ok=True)

        with main.get_meta_connection() as conn:
            root_id = main.get_root_folder_id(conn)
            root_feeds = main.get_folder_feed_urls(conn, root_id)

        assert FEED in root_feeds
        assert FEED2 in root_feeds  # the orphan is now covered by root actions


# ---------------------------------------------------------------------------
# Site C — delete_folder
# ---------------------------------------------------------------------------

class TestDeleteFolder:
    def test_delete_folder_websub_unsubscribes_orphaned_feeds(self, env, monkeypatch):
        fid = _make_child_folder("ToDelete")
        _add_feed_to_folder(FEED, fid)
        ws_mock = MagicMock()
        monkeypatch.setattr(main, "websub_service", ws_mock)
        monkeypatch.setattr(main.starred_archive_service, "force_archive_pending_for_feed", MagicMock(return_value=0))
        main.delete_folder(fid)
        ws_mock.unsubscribe.assert_called_once_with(FEED, tenancy.DEFAULT_USER_ID)

    def test_delete_folder_force_archives_before_deletion(self, env, monkeypatch):
        fid = _make_child_folder("ToDelete2")
        _add_feed_to_folder(FEED, fid)
        archive_mock = MagicMock(return_value=3)
        monkeypatch.setattr(main.starred_archive_service, "force_archive_pending_for_feed", archive_mock)
        monkeypatch.setattr(main, "websub_service", None)
        main.delete_folder(fid)
        archive_mock.assert_called_once_with(FEED)

    def test_delete_folder_does_not_unsubscribe_cross_folder_feed(self, env, monkeypatch):
        """A feed that also lives in another folder must not be unsubscribed."""
        fid = _make_child_folder("ToDel3")
        fid2 = _make_child_folder("Keeper")
        _add_feed_to_folder(FEED, fid)
        _add_feed_to_folder(FEED, fid2)
        ws_mock = MagicMock()
        monkeypatch.setattr(main, "websub_service", ws_mock)
        monkeypatch.setattr(main.starred_archive_service, "force_archive_pending_for_feed", MagicMock(return_value=0))
        main.delete_folder(fid)
        ws_mock.unsubscribe.assert_not_called()

    def test_delete_folder_move_reassigns_feeds_to_target(self, env, monkeypatch):
        fid = _make_child_folder("MoveFrom")
        target = _make_child_folder("MoveTo")
        _add_feed_to_folder(FEED, fid)
        ws_mock = MagicMock()
        monkeypatch.setattr(main, "websub_service", ws_mock)
        deleted, unsubbed, moved = main.delete_folder(
            fid, feed_action="move", move_to_folder_id=target
        )
        assert (unsubbed, moved) == (0, 1)
        ws_mock.unsubscribe.assert_not_called()
        with main.get_meta_connection() as conn:
            rows = conn.execute(
                "SELECT folder_id FROM folder_feeds WHERE feed_url = ?", (FEED,)
            ).fetchall()
        assert [int(r["folder_id"]) for r in rows] == [target]

    def test_delete_folder_move_to_uncategorized_leaves_feed_folderless(self, env, monkeypatch):
        fid = _make_child_folder("MoveFrom2")
        _add_feed_to_folder(FEED, fid)
        ws_mock = MagicMock()
        monkeypatch.setattr(main, "websub_service", ws_mock)
        main.delete_folder(
            fid, feed_action="move", move_to_folder_id=main.UNCATEGORIZED_FOLDER_ID
        )
        ws_mock.unsubscribe.assert_not_called()
        with main.get_meta_connection() as conn:
            rows = conn.execute(
                "SELECT 1 FROM folder_feeds WHERE feed_url = ?", (FEED,)
            ).fetchall()
        assert rows == []  # folderless => Uncategorized
        with main.get_reader() as reader:
            assert any(str(f.url) == FEED for f in reader.get_feeds())  # still subscribed

    def test_delete_folder_move_into_self_rejected(self, env, monkeypatch):
        fid = _make_child_folder("MoveSelf")
        _add_feed_to_folder(FEED, fid)
        monkeypatch.setattr(main, "websub_service", MagicMock())
        with pytest.raises(ValueError):
            main.delete_folder(fid, feed_action="move", move_to_folder_id=fid)


# ---------------------------------------------------------------------------
# Site D — deduplicate_feeds (same-folder + cross-folder) and E (upgrade)
# ---------------------------------------------------------------------------

class TestDeduplicateWebSub:
    """dedup/upgrade must WebSub-unsubscribe the REMOVED url."""

    def _setup_same_folder_dup(self, fid):
        """Add both FEED and FEED2 to fid, so dedup sees a same-folder dup."""
        _add_feed_to_folder(FEED, fid)
        _add_feed_to_folder(FEED2, fid)

    def test_same_folder_dedup_unsubscribes_removed_url(self, env, monkeypatch):
        fid = _make_child_folder("Dedup1")
        self._setup_same_folder_dup(fid)
        ws_mock = MagicMock()
        monkeypatch.setattr(main, "websub_service", ws_mock)
        monkeypatch.setattr(main.starred_archive_service, "force_archive_pending_for_feed", MagicMock(return_value=0))

        # purge_orphaned_feed for dedup uses archive_pending=False and rescue_to
        # — only the WebSub step matters here.
        with main.get_meta_connection() as conn:
            conn.execute(
                "DELETE FROM folder_feeds WHERE folder_id = ? AND feed_url = ?",
                (fid, FEED2),
            )
        with main.get_reader() as reader:
            with main.get_meta_connection() as conn:
                main.purge_orphaned_feed(reader, conn, FEED2, archive_pending=False, rescue_to=FEED)
        ws_mock.unsubscribe.assert_called_once_with(FEED2, tenancy.DEFAULT_USER_ID)

    def test_dedup_does_not_archive_pending_for_removed_url(self, env, monkeypatch):
        """archive_pending=False means force_archive is NOT called on dedup removal."""
        fid = _make_child_folder("Dedup2")
        self._setup_same_folder_dup(fid)
        archive_mock = MagicMock(return_value=0)
        monkeypatch.setattr(main.starred_archive_service, "force_archive_pending_for_feed", archive_mock)
        monkeypatch.setattr(main, "websub_service", None)
        with main.get_meta_connection() as conn:
            conn.execute(
                "DELETE FROM folder_feeds WHERE folder_id = ? AND feed_url = ?",
                (fid, FEED2),
            )
        with main.get_reader() as reader:
            with main.get_meta_connection() as conn:
                main.purge_orphaned_feed(reader, conn, FEED2, archive_pending=False, rescue_to=FEED)
        archive_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Part B — get_push_active_feed_urls
# ---------------------------------------------------------------------------

class TestPushActiveFeedUrls:
    def test_returns_empty_set_when_websub_disabled(self, env, monkeypatch):
        monkeypatch.setattr(main, "websub_service", None)
        assert main.get_push_active_feed_urls() == set()

    def test_returns_verified_feed_with_hub(self, env, monkeypatch):
        # Ensure websub_service is truthy (value just needs to be non-None).
        monkeypatch.setattr(main, "websub_service", MagicMock())
        conn = main.get_websub_connection()
        conn.execute(
            "INSERT OR REPLACE INTO websub_subscriptions"
            " (feed_url, hub_url, secret, subscribed_at, verified, expires_at, lease_seconds, hub_tried_at)"
            " VALUES (?, ?, ?, 0, 1, ?, 604800, 0)",
            (FEED, "https://hub.test/", "secret", time.time() + 99999),
        )
        conn.commit()
        urls = main.get_push_active_feed_urls()
        assert FEED in urls

    def test_excludes_unverified_subscription(self, env, monkeypatch):
        monkeypatch.setattr(main, "websub_service", MagicMock())
        conn = main.get_websub_connection()
        conn.execute(
            "INSERT OR REPLACE INTO websub_subscriptions"
            " (feed_url, hub_url, secret, subscribed_at, verified, expires_at, lease_seconds, hub_tried_at)"
            " VALUES (?, ?, ?, 0, 0, ?, 604800, 0)",
            (FEED2, "https://hub.test/", "secret2", time.time() + 99999),
        )
        conn.commit()
        urls = main.get_push_active_feed_urls()
        assert FEED2 not in urls

    def test_excludes_verified_without_hub(self, env, monkeypatch):
        monkeypatch.setattr(main, "websub_service", MagicMock())
        conn = main.get_websub_connection()
        conn.execute(
            "INSERT OR REPLACE INTO websub_subscriptions"
            " (feed_url, hub_url, secret, subscribed_at, verified, expires_at, lease_seconds, hub_tried_at)"
            " VALUES (?, NULL, ?, 0, 1, ?, 604800, 0)",
            ("https://no-hub.test/feed", "secret3", time.time() + 99999),
        )
        conn.commit()
        urls = main.get_push_active_feed_urls()
        assert "https://no-hub.test/feed" not in urls

    def test_returns_empty_set_on_db_error(self, env, monkeypatch):
        monkeypatch.setattr(main, "websub_service", MagicMock())
        # Override get_websub_connection to raise.
        monkeypatch.setattr(main, "get_websub_connection", lambda: (_ for _ in ()).throw(Exception("db gone")))
        result = main.get_push_active_feed_urls()
        assert result == set()


# ---------------------------------------------------------------------------
# _migrate_curation — tags + stars move onto the survivor (dedup consolidation)
# ---------------------------------------------------------------------------

def _add_entry(feed_url: str, entry_id: str, link: str) -> None:
    with main.get_reader() as reader:
        reader.add_entry({"feed_url": feed_url, "id": entry_id, "title": entry_id, "link": link})


def _tag_entry(feed_url: str, entry_id: str, tag: str) -> None:
    with main.get_reader() as reader:
        reader.set_tag((feed_url, entry_id), f"{main.MANUAL_TAG_KEY_PREFIX}{tag}")


def _star_entry(feed_url: str, entry_id: str) -> None:
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id, saved_at) VALUES (?, ?, ?)",
            (feed_url, entry_id, "2020-01-01T00:00:00Z"),
        )
        conn.commit()


class TestMigrateCuration:
    def test_synth_when_survivor_lacks_entry(self, env):
        """A tagged+starred source entry absent from the survivor is synthesized
        into it, carrying the tag and star; source star row is removed."""
        _add_feed_to_folder(FEED, _root_folder_id())    # survivor, no matching entry
        _add_feed_to_folder(FEED2, _root_folder_id())   # source
        _add_entry(FEED2, "e1", "https://example.test/a")
        _tag_entry(FEED2, "e1", "python")
        _star_entry(FEED2, "e1")

        with main.get_reader() as reader:
            with main.get_meta_connection() as conn:
                counts = main._migrate_curation(reader, conn, FEED2, FEED)

        assert counts == {"tags": 1, "stars": 1, "synth": 1}
        with main.get_reader() as reader:
            keys = [main._extract_tag_key(t) for t in reader.get_tags((FEED, "e1"))]
        assert f"{main.MANUAL_TAG_KEY_PREFIX}python" in keys
        with main.get_meta_connection() as conn:
            surv = conn.execute(
                "SELECT 1 FROM saved_entries WHERE feed_url=? AND entry_id=?", (FEED, "e1")
            ).fetchone()
            src = conn.execute(
                "SELECT 1 FROM saved_entries WHERE feed_url=?", (FEED2,)
            ).fetchone()
        assert surv is not None
        assert src is None  # moved off the source feed

    def test_guid_match_attaches_to_existing_entry(self, env):
        """When the survivor already holds the same GUID, the tag attaches to that
        entry instead of synthesizing a duplicate."""
        _add_feed_to_folder(FEED, _root_folder_id())
        _add_feed_to_folder(FEED2, _root_folder_id())
        _add_entry(FEED, "e1", "https://example.test/a")   # survivor already has e1
        _add_entry(FEED2, "e1", "https://example.test/a")  # source
        _tag_entry(FEED2, "e1", "git")

        with main.get_reader() as reader:
            with main.get_meta_connection() as conn:
                counts = main._migrate_curation(reader, conn, FEED2, FEED)

        assert counts["tags"] == 1
        assert counts["synth"] == 0
        with main.get_reader() as reader:
            keys = [main._extract_tag_key(t) for t in reader.get_tags((FEED, "e1"))]
        assert f"{main.MANUAL_TAG_KEY_PREFIX}git" in keys

    def test_no_curation_is_noop(self, env):
        _add_feed_to_folder(FEED, _root_folder_id())
        _add_feed_to_folder(FEED2, _root_folder_id())
        _add_entry(FEED2, "e1", "https://example.test/a")  # untagged, unstarred
        with main.get_reader() as reader:
            with main.get_meta_connection() as conn:
                counts = main._migrate_curation(reader, conn, FEED2, FEED)
        assert counts == {"tags": 0, "stars": 0, "synth": 0}
