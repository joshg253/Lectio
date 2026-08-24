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
- purge_orphaned_feed via the dedup/combine call pattern (Site D): websub-unsubscribes the removed URL
- get_push_active_feed_urls (Part B): verified vs pending/no-hub
"""
from __future__ import annotations

import datetime as dt
import time
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import main
from services import tenancy

FEED = "https://example.test/feed"
FEED2 = "https://example.test/feed/"  # slash variant for dedup tests
BASE = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _reset_pools() -> None:
    main.close_thread_db_pools()
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


def _csrf_client() -> tuple[TestClient, str]:
    """A TestClient with a session, plus its CSRF token for form posts."""
    import base64
    import json

    from itsdangerous import TimestampSigner

    client = TestClient(main.app)
    client.get("/healthz")  # establishes the session + token
    cookie = client.cookies.get("session")
    signer = TimestampSigner(main.SESSION_SECRET_KEY)
    session_data = json.loads(base64.b64decode(signer.unsign(cookie, max_age=main.SESSION_MAX_AGE_SECONDS)))
    return client, session_data["csrf_token"]


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
# Site D — dedup/upgrade's purge_orphaned_feed call pattern (same-folder,
# cross-folder, and combine/upgrade all route through it identically)
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

        assert counts == {"tags": 1, "stars": 1, "synth": 1, "archives": 0, "entries": 1}
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

    def test_an_uncurated_entry_moves_too(self, env):
        """Was `test_no_curation_is_noop`, and the no-op was the bug: combining
        two feeds silently dropped every post that carried no tag, star or
        capture. A combine says "these two are the same feed", so the entries
        move whatever their state."""
        _add_feed_to_folder(FEED, _root_folder_id())
        _add_feed_to_folder(FEED2, _root_folder_id())
        _add_entry(FEED2, "e1", "https://example.test/a")  # untagged, unstarred
        with main.get_reader() as reader:
            with main.get_meta_connection() as conn:
                counts = main._migrate_curation(reader, conn, FEED2, FEED)
            moved = reader.get_entry((FEED, "e1"), None)
        assert counts == {"tags": 0, "stars": 0, "synth": 1, "archives": 0, "entries": 1}
        assert moved is not None, "the post must land on the survivor"
        assert moved.read is False


class TestDuplicateScanGroupsAcrossSchemes:
    """A domain alias rewrites the host but keeps the scheme, so a legacy
    `http://` subscription and its `https://` twin normalized to different
    strings and never grouped. Two dead tapastic.com feeds sat beside their live
    tapas.io twins, failing every refresh, unflagged."""

    def _keys(self, dup):
        return {(d["keep"], d["remove"]) for d in dup["same_folder"]}

    def test_a_scheme_only_pair_is_found_and_https_survives(self, env):
        root = _root_folder_id()
        _add_feed_to_folder("http://azius.com/blog/feed", root)
        _add_feed_to_folder("https://azius.com/blog/feed", root)
        import json
        dup = json.loads(main.get_feed_duplicates().body)
        assert ("https://azius.com/blog/feed", "http://azius.com/blog/feed") in self._keys(dup)

    def test_a_domain_alias_across_schemes_groups_too(self, env):
        """The reported case: http tapastic husk vs https tapas.io twin."""
        root = _root_folder_id()
        _add_feed_to_folder("http://tapastic.com/rss/series/4879", root)
        _add_feed_to_folder("https://tapas.io/rss/series/4879", root)
        import json
        dup = json.loads(main.get_feed_duplicates().body)
        assert ("https://tapas.io/rss/series/4879",
                "http://tapastic.com/rss/series/4879") in self._keys(dup)

    def test_the_survivor_is_always_a_subscribed_url(self, env):
        """`keep` used to be the canonical *string*, which is not always one of
        the variants — then every variant was offered for removal against a URL
        nobody is subscribed to."""
        root = _root_folder_id()
        subscribed = {"http://example.test/feed/", "https://example.test/feed/"}
        for u in subscribed:
            _add_feed_to_folder(u, root)
        import json
        dup = json.loads(main.get_feed_duplicates().body)
        pairs = self._keys(dup)
        assert pairs, "the pair must be detected"
        for keep, remove in pairs:
            assert keep in subscribed, f"survivor {keep} is not subscribed"
            assert remove in subscribed
            assert keep != remove

    def test_unrelated_feeds_are_not_grouped(self, env):
        root = _root_folder_id()
        _add_feed_to_folder("https://a.test/feed", root)
        _add_feed_to_folder("https://b.test/feed", root)
        import json
        dup = json.loads(main.get_feed_duplicates().body)
        assert dup["same_folder"] == [] and dup["cross_folder"] == []

    def test_a_trailing_slash_pair_still_keeps_the_canonical_spelling(self, env):
        """The behaviour this function already had must not regress."""
        root = _root_folder_id()
        _add_feed_to_folder("https://slash.test/feed", root)
        _add_feed_to_folder("https://slash.test/feed/", root)
        import json
        dup = json.loads(main.get_feed_duplicates().body)
        assert ("https://slash.test/feed", "https://slash.test/feed/") in self._keys(dup)

    def test_a_www_pair_is_grouped_and_bare_host_survives(self, env):
        """Reported 2026-08-10: deathbulge.com/rss.xml and
        www.deathbulge.com/rss.xml are the same feed, not two -- caught
        instead as noise by the by-title tier, when the regular scan should
        have found it directly."""
        root = _root_folder_id()
        _add_feed_to_folder("https://deathbulge.com/rss.xml", root)
        _add_feed_to_folder("https://www.deathbulge.com/rss.xml", root)
        import json
        dup = json.loads(main.get_feed_duplicates().body)
        assert ("https://deathbulge.com/rss.xml", "https://www.deathbulge.com/rss.xml") in self._keys(dup)


class TestDuplicateScanGroupsBySameTitle:
    """The URL-scheme grouping only catches variants of ONE address. It can't
    catch the same publication subscribed under two genuinely different
    addresses (a Tumblr and a Tapas copy of the same webcomic) -- feed title
    is the signal that finds those instead. Advisory only: never pre-checked,
    no auto-apply action, since a same-title pair can legitimately be two
    different things (a site's blog and its own podcast)."""

    def _set_title(self, feed_url: str, title: str) -> None:
        with main.get_reader() as reader:
            reader.set_feed_user_title(feed_url, title)

    def _title_groups(self):
        import json
        return json.loads(main.get_feed_duplicates().body)["title_groups"]

    def test_two_feeds_sharing_a_title_are_grouped(self, env):
        root = _root_folder_id()
        _add_feed_to_folder("https://tumblr.test/comic", root)
        _add_feed_to_folder("https://tapas.test/comic", root)
        self._set_title("https://tumblr.test/comic", "Cryptid Club")
        self._set_title("https://tapas.test/comic", "Cryptid Club")
        groups = self._title_groups()
        assert len(groups) == 1
        assert groups[0]["title"] == "Cryptid Club"
        assert {f["feed_url"] for f in groups[0]["feeds"]} == {
            "https://tumblr.test/comic", "https://tapas.test/comic",
        }

    def test_title_matching_is_case_insensitive(self, env):
        root = _root_folder_id()
        _add_feed_to_folder("https://a.test/feed", root)
        _add_feed_to_folder("https://b.test/feed", root)
        self._set_title("https://a.test/feed", "Nine Inch Nails")
        self._set_title("https://b.test/feed", "nine inch nails")
        groups = self._title_groups()
        assert len(groups) == 1
        assert {f["feed_url"] for f in groups[0]["feeds"]} == {
            "https://a.test/feed", "https://b.test/feed",
        }

    def test_a_unique_title_is_not_grouped(self, env):
        root = _root_folder_id()
        _add_feed_to_folder("https://solo.test/feed", root)
        self._set_title("https://solo.test/feed", "Only One Of These")
        assert self._title_groups() == []

    def test_a_generic_title_shared_by_many_unrelated_feeds_is_excluded(self, env):
        """The generic-title floor: "news" on 7 unrelated sites is noise, not
        a duplicate -- every genuine match measured at 2-3 feeds."""
        root = _root_folder_id()
        for i in range(6):
            url = f"https://site{i}.test/feed"
            _add_feed_to_folder(url, root)
            self._set_title(url, "News")
        assert self._title_groups() == []

    def test_folder_names_are_included_per_feed(self, env):
        folder_a = _make_child_folder("Comics")
        folder_b = _make_child_folder("Webtoons")
        _add_feed_to_folder("https://a.test/feed", folder_a)
        _add_feed_to_folder("https://b.test/feed", folder_b)
        self._set_title("https://a.test/feed", "Sarah's Scribbles")
        self._set_title("https://b.test/feed", "Sarah's Scribbles")
        groups = self._title_groups()
        assert len(groups) == 1
        by_url = {f["feed_url"]: f for f in groups[0]["feeds"]}
        assert by_url["https://a.test/feed"]["folders"][0]["name"] == "Comics"
        assert by_url["https://b.test/feed"]["folders"][0]["name"] == "Webtoons"

    def test_youtube_feeds_are_excluded_as_noise(self, env):
        """A creator's blog and their YouTube channel routinely share a title
        (the channel name) -- subscribing to both is normal, not a duplicate.
        Reported 2026-08-10: wsdot.wa.gov + Minecraft Forum + two genuine
        tosecdev.org duplicates, all literally titled "News" -- YouTube is the
        same class of false positive."""
        root = _root_folder_id()
        _add_feed_to_folder("https://blog.test/feed", root)
        _add_feed_to_folder(
            "https://www.youtube.com/feeds/videos.xml?channel_id=UCabc123", root,
        )
        self._set_title("https://blog.test/feed", "Some Creator")
        self._set_title(
            "https://www.youtube.com/feeds/videos.xml?channel_id=UCabc123", "Some Creator",
        )
        assert self._title_groups() == []


class TestDuplicateScanQueryDifferingPairs:
    """Same host+path, different query -- a real duplicate class
    (tosecdev.org's ?type=atom vs ?type=rss), but a query param can also
    select genuinely different content (a WordPress category feed), so this
    tier is never merged into same_folder/cross_folder's all-included
    "Remove duplicates" click -- each pair needs its own explicit inclusion,
    checked client-side. Reported 2026-08-10."""

    def _query_pairs(self):
        import json
        return json.loads(main.get_feed_duplicates().body)["query_pairs"]

    def test_a_differing_query_pair_with_an_unrecognized_selector_is_found(self, env):
        """A param+value combo that isn't a recognized format selector still
        needs a human, not an auto-fold -- exactly the WordPress-category
        risk this tier exists for."""
        root = _root_folder_id()
        _add_feed_to_folder("https://example.test/news?variant=full", root)
        _add_feed_to_folder("https://example.test/news?variant=summary", root)
        pairs = self._query_pairs()
        assert len(pairs) == 1
        urls = {pairs[0]["keep"], pairs[0]["remove"]}
        assert urls == {
            "https://example.test/news?variant=full",
            "https://example.test/news?variant=summary",
        }

    def test_recognized_format_selector_pairs_are_promoted_to_auto_handling(self, env):
        """tosecdev.org's ?type=atom/?type=rss and paizo.com's
        ?feed=json1/?feed=rss (reported 2026-08-10) are recognized format
        selectors -- normalize_feed_url folds them to one canonical form, so
        they pick up the existing same_folder auto-handling instead of
        landing in the never-pre-checked query_pairs tier."""
        root = _root_folder_id()
        _add_feed_to_folder("https://tosecdev.test/news?format=feed&type=atom", root)
        _add_feed_to_folder("https://tosecdev.test/news?format=feed&type=rss", root)
        _add_feed_to_folder("https://paizo.test/blog?feed=json1", root)
        _add_feed_to_folder("https://paizo.test/blog?feed=rss", root)
        import json
        dup = json.loads(main.get_feed_duplicates().body)
        same_folder_urls = {(d["keep"], d["remove"]) for d in dup["same_folder"]}
        assert any("tosecdev.test" in k for k, _r in same_folder_urls)
        assert any("paizo.test" in k for k, _r in same_folder_urls)
        assert dup["query_pairs"] == []

    def test_youtube_feeds_are_excluded(self, env):
        root = _root_folder_id()
        _add_feed_to_folder(
            "https://www.youtube.com/feeds/videos.xml?channel_id=UCabc", root,
        )
        _add_feed_to_folder(
            "https://www.youtube.com/feeds/videos.xml?channel_id=UCdef", root,
        )
        assert self._query_pairs() == []

    def test_deviantart_gallery_feeds_are_excluded(self, env):
        """DeviantArt's native gallery RSS is one shared endpoint for every
        artist, differing only by ?q=gallery:<user> -- reported 2026-08-10:
        grouped dozens of genuinely distinct subscriptions as "duplicates"."""
        root = _root_folder_id()
        _add_feed_to_folder(
            "https://backend.deviantart.com/rss.xml?q=gallery:rantz&type=deviation", root,
        )
        _add_feed_to_folder(
            "https://backend.deviantart.com/rss.xml?q=gallery:yuumei&type=deviation", root,
        )
        assert self._query_pairs() == []

    def test_a_pair_already_caught_by_scheme_or_www_fold_is_not_listed_twice(self, env):
        """These differ only by scheme -- already same_folder/cross_folder's
        job; listing them again here too would be double coverage."""
        root = _root_folder_id()
        _add_feed_to_folder("http://plain.test/feed", root)
        _add_feed_to_folder("https://plain.test/feed", root)
        assert self._query_pairs() == []

    def test_unrelated_paths_are_not_grouped(self, env):
        root = _root_folder_id()
        _add_feed_to_folder("https://site.test/feed-a", root)
        _add_feed_to_folder("https://site.test/feed-b", root)
        assert self._query_pairs() == []


class TestRemovalLeavesNoGhostInTheTree:
    """Removing a feed deleted it from reader but left `folder_feeds` pointing
    at it, so Settings → Feeds went on listing a subscription that no longer
    exists — rendered from the folder row alone, with a failing badge. The 29
    article-URL husks rehomed to Saved Articles on 2026-08-06 all came back as
    ghosts this way and were reported as "non-feeds I thought we cleared out".
    """

    def _folder_rows(self, url):
        with main.get_meta_connection() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM folder_feeds WHERE feed_url = ?", (url,)
            ).fetchone()[0]

    def test_purging_a_feed_removes_its_folder_row(self, env):
        _add_feed_to_folder(FEED, _root_folder_id())
        assert self._folder_rows(FEED) == 1
        with main.get_reader() as reader:
            with main.get_meta_connection() as conn:
                main.purge_orphaned_feed(reader, conn, FEED, archive_pending=False)
                conn.commit()
            assert reader.get_feed(FEED, None) is None
        assert self._folder_rows(FEED) == 0, "a folder row with no feed is a ghost in the tree"

    def test_other_feeds_keep_their_folder_rows(self, env):
        root = _root_folder_id()
        _add_feed_to_folder(FEED, root)
        _add_feed_to_folder(FEED2, root)
        with main.get_reader() as reader:
            with main.get_meta_connection() as conn:
                main.purge_orphaned_feed(reader, conn, FEED, archive_pending=False)
                conn.commit()
        assert self._folder_rows(FEED) == 0
        assert self._folder_rows(FEED2) == 1


class TestLazyFeedTitles:
    """Feeds titled just "News", "Updates", etc are indistinguishable once
    several show up together (reported 2026-08-10). Detection is a small
    exact-match denylist -- never a length heuristic, so a short but
    meaningful title ("XKCD") is never flagged. Advisory only: the frontend
    renders an editable per-row suggestion, nothing is applied automatically."""

    def _set_title(self, feed_url: str, title: str) -> None:
        with main.get_reader() as reader:
            reader.set_feed_user_title(feed_url, title)

    def _lazy_titles(self):
        import json
        return json.loads(main.get_lazy_titles().body)["lazy_titles"]

    def test_a_generic_title_is_flagged_with_a_suggested_rename(self, env):
        _add_feed_to_folder("https://tosecdev.org/feed", _root_folder_id())
        self._set_title("https://tosecdev.org/feed", "News")
        results = self._lazy_titles()
        assert len(results) == 1
        assert results[0]["feed_url"] == "https://tosecdev.org/feed"
        assert results[0]["title"] == "News"
        assert results[0]["suggested_title"] == "Tosecdev - News"

    def test_matching_is_case_and_whitespace_insensitive(self, env):
        _add_feed_to_folder("https://example.test/feed", _root_folder_id())
        self._set_title("https://example.test/feed", "  updates  ")
        results = self._lazy_titles()
        assert len(results) == 1

    def test_a_meaningful_short_title_is_not_flagged(self, env):
        _add_feed_to_folder("https://xkcd.test/feed", _root_folder_id())
        self._set_title("https://xkcd.test/feed", "XKCD")
        assert self._lazy_titles() == []

    def test_www_and_tld_are_stripped_from_the_suggested_name(self, env):
        _add_feed_to_folder("https://www.some-cool-blog.co.uk/feed", _root_folder_id())
        self._set_title("https://www.some-cool-blog.co.uk/feed", "Blog")
        results = self._lazy_titles()
        assert results[0]["suggested_title"] == "Some Cool Blog - Blog"

    def test_folder_names_are_included(self, env):
        folder = _make_child_folder("Comics")
        _add_feed_to_folder("https://a.test/feed", folder)
        self._set_title("https://a.test/feed", "Posts")
        results = self._lazy_titles()
        assert results[0]["folders"][0]["name"] == "Comics"


class TestFormatUpgradeAlternates:
    """The Upgrade tier's `alternates` are same-family format-selector swaps
    (rss2 -> atom, say) -- a real, near-guaranteed-to-exist option on sites
    like WordPress, not a guess. Reported 2026-08-10: this used to guess at
    a JSON Feed URL instead, which came back wrong twice live (404, then
    500) while "wordpress can have ?feed=atom which work, and those aren't
    being suggested but non-existent json are??" -- same-family swaps within
    the enumerated rss/rss2/atom set are the actual reliable signal."""

    def _upgradable(self):
        import json
        return json.loads(main.get_feed_duplicates().body)["upgradable"]

    def test_a_feed_param_gets_same_family_alternates(self, env):
        _add_feed_to_folder("https://example.test/blog?feed=rss2", _root_folder_id())
        results = self._upgradable()
        assert len(results) == 1
        assert results[0]["current"] == "https://example.test/blog?feed=rss2"
        assert set(results[0]["alternates"]) == {
            "https://example.test/blog?feed=atom",
            "https://example.test/blog?feed=rss",
        }

    def test_an_alt_param_gets_same_family_alternates(self, env):
        _add_feed_to_folder("https://example.test/feed?alt=rss", _root_folder_id())
        results = self._upgradable()
        assert "https://example.test/feed?alt=atom" in results[0]["alternates"]

    def test_no_alternate_already_subscribed_is_repeated(self, env):
        root = _root_folder_id()
        _add_feed_to_folder("https://example.test/blog?feed=rss2", root)
        _add_feed_to_folder("https://example.test/blog?feed=atom", root)
        results = self._upgradable()
        assert "https://example.test/blog?feed=atom" not in results[0]["alternates"]

    def test_root_level_feed_param_still_offers_alternates(self, env):
        """The stripped-default candidate is skipped for a bare-root URL
        (WordPress's ?feed=rss2 at "/" -- stripping it leaves nothing but
        the homepage), but same-family alternates keep the query intact and
        are still offered."""
        _add_feed_to_folder("https://example.test/?feed=rss2", _root_folder_id())
        results = self._upgradable()
        assert len(results) == 1
        assert results[0]["upgrade_to"] is None
        assert set(results[0]["alternates"]) == {
            "https://example.test/?feed=atom",
            "https://example.test/?feed=rss",
        }

    def test_no_candidate_without_a_format_selector(self, env):
        _add_feed_to_folder("https://example.test/feed?category=news", _root_folder_id())
        assert self._upgradable() == []

    def test_an_unrecognized_selector_value_does_not_count_as_a_second_match(self, env):
        # format=feed isn't a recognized selector *value* (only the param
        # name is on the allowlist), so only type=rss counts as the one
        # match -- alternates still fire.
        _add_feed_to_folder("https://example.test/feed?format=feed&type=rss", _root_folder_id())
        results = self._upgradable()
        assert len(results) == 1
        assert "https://example.test/feed?format=feed&type=atom" in results[0]["alternates"]

    def test_two_recognized_format_params_are_too_ambiguous_for_alternates(self, env):
        _add_feed_to_folder("https://example.test/feed?alt=rss&type=atom", _root_folder_id())
        results = self._upgradable()
        assert results[0]["alternates"] == []


class TestCombineFeedsSurvivorFolderPlacement:
    """/feeds/combine's survivor is normally an existing subscription (every
    detected-duplicate tier picks keep/remove from feeds already foldered) --
    but the Upgrade tier's Compare candidates are brand-new URLs nobody has
    subscribed to yet. Without placing the survivor into the sources' folders
    it would land subscribed-but-folderless (invisible in the tree)."""

    def _folders_of(self, url):
        with main.get_meta_connection() as conn:
            return {
                r[0] for r in conn.execute(
                    "SELECT folder_id FROM folder_feeds WHERE feed_url = ?", (url,)
                )
            }

    def test_new_survivor_inherits_the_sources_folders(self, env):
        folder = _make_child_folder("Comics")
        _add_feed_to_folder(FEED, folder)
        new_url = "https://example.test/feed?feed=json1"
        result = main.combine_feeds_route(
            None, survivor_url=new_url, source_url=[FEED], move_unread="",
        )
        import json as _json
        body = _json.loads(result.body)
        assert body["ok"] is True
        assert self._folders_of(new_url) == {folder}
        with main.get_reader() as reader:
            assert reader.get_feed(new_url, None) is not None

    def test_already_subscribed_survivor_keeps_its_own_folders(self, env):
        """Existing tiers (title/same-folder/cross-folder/query) always pass an
        already-foldered survivor -- this must stay a no-op for them."""
        folder_a = _make_child_folder("A")
        folder_b = _make_child_folder("B")
        _add_feed_to_folder(FEED, folder_a)
        _add_feed_to_folder(FEED2, folder_b)
        main.combine_feeds_route(
            None, survivor_url=FEED, source_url=[FEED2], move_unread="",
        )
        assert self._folders_of(FEED) == {folder_a}


class TestContentIdenticalFlag:
    """`content_identical` gates the "suggested keep" bias in same_folder and
    cross_folder. Reported 2026-08-10 (freac.org): ?type=rss carries
    summaries only, ?type=atom carries full text -- a format-selector swap
    is a real content difference, not a cosmetic one like scheme/www, so the
    tie-break's pick must never be presented as a suggestion there."""

    def _duplicates(self):
        import json
        return json.loads(main.get_feed_duplicates().body)

    def test_scheme_only_variant_is_content_identical(self, env):
        fid = _make_child_folder("Scheme")
        _add_feed_to_folder("https://example.test/feed", fid)
        _add_feed_to_folder("http://example.test/feed", fid)
        same = self._duplicates()["same_folder"]
        assert len(same) == 1
        assert same[0]["content_identical"] is True

    def test_format_selector_variant_is_not_content_identical(self, env):
        fid = _make_child_folder("Format")
        _add_feed_to_folder("https://www.freac.org/index.php?format=feed&type=rss", fid)
        _add_feed_to_folder("https://www.freac.org/index.php?format=feed&type=atom", fid)
        same = self._duplicates()["same_folder"]
        assert len(same) == 1
        assert same[0]["content_identical"] is False

    def test_cross_folder_format_selector_variant_is_not_content_identical(self, env):
        folder_a = _make_child_folder("A")
        folder_b = _make_child_folder("B")
        _add_feed_to_folder("https://www.freac.org/index.php?format=feed&type=rss", folder_a)
        _add_feed_to_folder("https://www.freac.org/index.php?format=feed&type=atom", folder_b)
        cross = self._duplicates()["cross_folder"]
        assert len(cross) == 1
        assert cross[0]["content_identical"] is False


class TestLazyFeedTitlesFeedburner:
    """FeedBurner is a proxy -- every burned feed shares feeds.feedburner.com
    regardless of the real site, so a domain-based name guess is useless
    there (reported 2026-08-10: "Feedburner -- Blog" for a Concept2 rowing
    feed). Falls back to the URL's last path segment (the feed slug) instead."""

    def _set_title(self, feed_url: str, title: str) -> None:
        with main.get_reader() as reader:
            reader.set_feed_user_title(feed_url, title)

    def _lazy_titles(self):
        import json
        return json.loads(main.get_lazy_titles().body)["lazy_titles"]

    def test_feedburner_uses_the_path_slug_not_the_host(self, env):
        _add_feed_to_folder("https://feeds.feedburner.com/concept2", _root_folder_id())
        self._set_title("https://feeds.feedburner.com/concept2", "Blog")
        results = self._lazy_titles()
        assert results[0]["suggested_title"] == "Concept2 - Blog"


class TestSiteNameFromSubtitle:
    """A feed's own <subtitle> beats a URL-based guess when present (reported
    2026-08-10: TOSEC's feed carries subtitle "TOSEC - The Old School
    Emulation Center", far better than anything tosecdev.org's domain
    guesses). Most subtitles pair a short name with a tagline via a
    separator; only the name half is used."""

    def test_name_dash_tagline_takes_the_name_half(self):
        assert main._site_name_from_subtitle(
            "TOSEC - The Old School Emulation Center"
        ) == "TOSEC"

    def test_name_colon_tagline_takes_the_name_half(self):
        assert main._site_name_from_subtitle("Kotaku: Gaming Reviews and News") == "Kotaku"

    def test_short_subtitle_with_no_separator_is_used_whole(self):
        assert main._site_name_from_subtitle("Concept2 Rowing") == "Concept2 Rowing"

    def test_long_sentence_with_no_separator_is_rejected(self):
        long_subtitle = "A very long description of what this site is about and why you should read it"
        assert main._site_name_from_subtitle(long_subtitle) is None

    def test_blank_subtitle_is_rejected(self):
        assert main._site_name_from_subtitle("   ") is None


class TestFormatUpgradeExcludesBareDomains:
    """Stripping a format-selector query has to leave something feed-shaped
    behind. WordPress's root-level ?feed=rss2 pattern is the failure case
    (reported 2026-08-10): the query IS the whole address there, not
    decoration on a working default -- stripping it leaves a bare domain
    that serves the HTML homepage, not a feed."""

    def _upgradable(self):
        import json
        return json.loads(main.get_feed_duplicates().body)["upgradable"]

    def test_root_level_feed_param_stripped_default_is_not_offered(self, env):
        """The stripped-default candidate specifically is excluded -- a
        same-family alternate (which keeps the query) can still be offered,
        covered by TestFormatUpgradeAlternates."""
        _add_feed_to_folder("https://example.test/?feed=rss2", _root_folder_id())
        results = self._upgradable()
        assert results[0]["upgrade_to"] is None

    def test_non_root_path_is_still_offered(self, env):
        _add_feed_to_folder("https://tosecdev.org/news?type=rss", _root_folder_id())
        results = self._upgradable()
        assert len(results) == 1
        assert results[0]["upgrade_to"] == "https://tosecdev.org/news"


class TestDedupDismissal:
    """"Not dupes" (reported 2026-08-10): a duplicate-scan group the user
    explicitly says isn't a dupe stops being suggested. Matched by the exact
    set of feed URLs shown in the group -- if the underlying feeds change,
    the dismissal naturally stops applying rather than silently hiding some
    other, unrelated group."""

    def _duplicates(self):
        import json
        return json.loads(main.get_feed_duplicates().body)

    def _dismiss(self, urls):
        import asyncio

        class _FakeRequest:
            async def json(self):
                return {"feed_urls": urls}

        return asyncio.run(main.dismiss_feed_duplicate(_FakeRequest()))

    def test_dismissed_same_folder_pair_is_excluded(self, env):
        fid = _make_child_folder("Comics")
        _add_feed_to_folder("https://example.test/feed", fid)
        _add_feed_to_folder("https://example.test/feed/", fid)
        before = self._duplicates()["same_folder"]
        assert len(before) == 1
        result = self._dismiss(["https://example.test/feed", "https://example.test/feed/"])
        import json
        assert json.loads(result.body)["ok"] is True
        after = self._duplicates()["same_folder"]
        assert after == []

    def test_dismissed_title_group_is_excluded(self, env):
        root = _root_folder_id()
        _add_feed_to_folder("https://tumblr.test/comic", root)
        _add_feed_to_folder("https://tapas.test/comic", root)
        with main.get_reader() as reader:
            reader.set_feed_user_title("https://tumblr.test/comic", "Cryptid Club")
            reader.set_feed_user_title("https://tapas.test/comic", "Cryptid Club")
        assert len(self._duplicates()["title_groups"]) == 1
        self._dismiss(["https://tumblr.test/comic", "https://tapas.test/comic"])
        assert self._duplicates()["title_groups"] == []

    def test_dismissal_is_order_independent(self, env):
        fid = _make_child_folder("Comics")
        _add_feed_to_folder("https://example.test/feed", fid)
        _add_feed_to_folder("https://example.test/feed/", fid)
        self._dismiss(["https://example.test/feed/", "https://example.test/feed"])
        assert self._duplicates()["same_folder"] == []

    def test_unrelated_pair_is_not_affected(self, env):
        fid = _make_child_folder("Comics")
        _add_feed_to_folder("https://example.test/feed", fid)
        _add_feed_to_folder("https://example.test/feed/", fid)
        _add_feed_to_folder("https://other.test/feed", fid)
        _add_feed_to_folder("https://other.test/feed/", fid)
        self._dismiss(["https://example.test/feed", "https://example.test/feed/"])
        after = self._duplicates()["same_folder"]
        assert len(after) == 1
        assert after[0]["remove"] == "https://other.test/feed/"

    def test_dismiss_requires_at_least_two_urls(self, env):
        result = self._dismiss(["https://example.test/feed"])
        assert result.status_code == 400


class TestCombineAutoDismisses:
    """A completed /feeds/combine always records a dismissal for the exact
    URL set involved, even when nothing was actually deleted. Reported
    2026-08-10: picking the already-subscribed "current" URL as Upgrade-tier
    survivor makes every "source" a never-subscribed candidate, so the purge
    loop is a structural no-op -- current's format-selector URL is
    untouched, and the next scan re-detects the identical group ("I combined
    it and it came back")."""

    def _dismissed_keys(self):
        with main.get_meta_connection() as conn:
            return {r[0] for r in conn.execute("SELECT dismiss_key FROM dedup_dismissed")}

    def test_no_op_combine_still_dismisses_the_group(self, env):
        """Survivor is the already-subscribed URL; sources were never
        subscribed -- nothing to delete, but the group must stop recurring."""
        _add_feed_to_folder("http://example.test/blog?feed=atom", _root_folder_id())
        result = main.combine_feeds_route(
            None,
            survivor_url="http://example.test/blog?feed=atom",
            source_url=["http://example.test/blog?feed=rss", "http://example.test/blog?feed=rss2"],
            move_unread="",
        )
        import json
        assert json.loads(result.body)["ok"] is True
        key = main._dedup_dismiss_key([
            "http://example.test/blog?feed=atom",
            "http://example.test/blog?feed=rss",
            "http://example.test/blog?feed=rss2",
        ])
        assert key in self._dismissed_keys()

    def test_dismissed_upgrade_group_no_longer_scanned(self, env):
        """Mirrors the real frontend flow: Compare/Combine act on the whole
        group (current + stripped default + both alternates), not just the
        two alternates."""
        _add_feed_to_folder("http://example.test/blog?feed=atom", _root_folder_id())
        main.combine_feeds_route(
            None,
            survivor_url="http://example.test/blog?feed=atom",
            source_url=[
                "http://example.test/blog",
                "http://example.test/blog?feed=rss",
                "http://example.test/blog?feed=rss2",
            ],
            move_unread="",
        )
        import json
        data = json.loads(main.get_feed_duplicates().body)
        currents = {d["current"] for d in data["upgradable"]}
        assert "http://example.test/blog?feed=atom" not in currents

    def test_real_merge_also_dismisses(self, env):
        """A genuine merge (source really was subscribed) also records a
        dismissal -- redundant with the source simply being gone, but
        harmless and keeps the guarantee uniform across every combine."""
        root = _root_folder_id()
        _add_feed_to_folder(FEED, root)
        _add_feed_to_folder(FEED2, root)
        main.combine_feeds_route(None, survivor_url=FEED, source_url=[FEED2], move_unread="")
        key = main._dedup_dismiss_key([FEED, FEED2])
        assert key in self._dismissed_keys()


# ---------------------------------------------------------------------------
# restar_curated_entries — the "show them at the top of the Inbox" option
# ---------------------------------------------------------------------------

class TestRestarCuratedEntries:
    """Unsubscribing a feed can bring its curated items back to the top.

    The Inbox orders by saved_entries.saved_at, so an item curated months ago
    sinks to wherever it was — the last place you look right after deciding to
    drop its feed. LXer prompted this: 1 starred (July 6) and 1 tagged-only.
    """

    def _entry(self, feed_url: str, entry_id: str, *, title: str = "t"):
        with main.get_reader() as reader:
            reader.add_feed(feed_url, allow_invalid_url=True, exist_ok=True)
            reader.add_entry({
                "feed_url": feed_url, "id": entry_id,
                "title": title, "link": entry_id,
            })

    def _saved_at(self, feed_url: str, entry_id: str):
        with main.get_meta_connection() as conn:
            row = conn.execute(
                "SELECT saved_at FROM saved_entries WHERE feed_url = ? AND entry_id = ?",
                (feed_url, entry_id),
            ).fetchone()
        return row["saved_at"] if row else None

    def test_a_tagged_but_unstarred_entry_gets_starred(self, env):
        """The case a plain date-bump cannot serve: there is no row to bump."""
        self._entry(FEED, "e-tagged")
        with main.get_reader() as reader:
            entry = reader.get_entry((FEED, "e-tagged"))
            reader.set_tag(entry.resource_id, "lectio.manual_tag.linux-stuff", "")

        assert main._entry_is_starred(FEED, "e-tagged") is False
        assert main.restar_curated_entries(FEED) == 1
        assert main._entry_is_starred(FEED, "e-tagged") is True
        assert self._saved_at(FEED, "e-tagged") is not None

    def test_an_already_starred_entry_is_restamped(self, env):
        """apply_star_state is INSERT OR IGNORE, so the old date would survive."""
        self._entry(FEED, "e-star")
        with main.get_meta_connection() as conn:
            conn.execute(
                "INSERT INTO saved_entries (feed_url, entry_id, saved_at)"
                " VALUES (?, ?, '2020-01-01 00:00:00')", (FEED, "e-star"))
            conn.commit()

        assert main.restar_curated_entries(FEED) == 1
        assert self._saved_at(FEED, "e-star") != "2020-01-01 00:00:00"

    def test_an_uncurated_entry_is_left_alone(self, env):
        """Scope guard: this stars what was already kept, it does not keep more."""
        self._entry(FEED, "e-plain")
        assert main.restar_curated_entries(FEED) == 0
        assert main._entry_is_starred(FEED, "e-plain") is False

    def test_another_feeds_stars_are_not_restamped(self, env):
        self._entry(FEED, "e-star")
        self._entry(FEED2, "other")
        with main.get_meta_connection() as conn:
            conn.execute(
                "INSERT INTO saved_entries (feed_url, entry_id, saved_at)"
                " VALUES (?, ?, '2020-01-01 00:00:00')", (FEED, "e-star"))
            conn.execute(
                "INSERT INTO saved_entries (feed_url, entry_id, saved_at)"
                " VALUES (?, ?, '2020-01-01 00:00:00')", (FEED2, "other"))
            conn.commit()

        main.restar_curated_entries(FEED)
        assert self._saved_at(FEED2, "other") == "2020-01-01 00:00:00"

    def test_unsubscribe_route_runs_it_only_when_asked(self, env, monkeypatch):
        """Off by default — unsubscribing must not reorder the Inbox unasked."""
        calls = []
        monkeypatch.setattr(main, "restar_curated_entries", lambda u: calls.append(u) or 0)
        monkeypatch.setattr(main, "websub_service", MagicMock())

        monkeypatch.setattr(main, "AUTH_ENABLED", False)
        _add_feed_to_folder(FEED, _root_folder_id())
        client, token = _csrf_client()
        r = client.post("/feeds/unsubscribe", data={
            "_csrf": token,
            "folder_id": _root_folder_id(), "feed_url": FEED,
        })
        assert r.status_code != 403
        assert calls == []

        _add_feed_to_folder(FEED, _root_folder_id())
        client.post("/feeds/unsubscribe", data={
            "_csrf": token,
            "folder_id": _root_folder_id(), "feed_url": FEED,
            "restar_curated": "1",
        })
        assert calls == [FEED]

    def test_route_restars_before_the_feed_is_removed(self, env, monkeypatch):
        """Order matters: the entries must still be readable when it runs."""
        seen = {}

        def _spy(feed_url):
            with main.get_reader() as reader:
                seen["feed_exists"] = reader.get_feed(feed_url, None) is not None
            return 0

        monkeypatch.setattr(main, "restar_curated_entries", _spy)
        monkeypatch.setattr(main, "websub_service", MagicMock())
        monkeypatch.setattr(main, "AUTH_ENABLED", False)
        _add_feed_to_folder(FEED, _root_folder_id())

        client, token = _csrf_client()
        client.post("/feeds/unsubscribe", data={
            "_csrf": token,
            "folder_id": _root_folder_id(), "feed_url": FEED, "restar_curated": "1",
        })
        assert seen["feed_exists"] is True


# ---------------------------------------------------------------------------
# drop_all_curation — "unsubscribe and drop everything"
# ---------------------------------------------------------------------------

class TestDropAllCuration:
    """The way out when the feed itself was the mistake.

    A star or a tag preserves the offline capture, so a plain unsubscribe leaves
    the posts behind in Saved as orphan archives. Dropping the keep signals
    first is what makes the removal actually remove.
    """

    @pytest.fixture(autouse=True)
    def _archive_schema(self, env):
        # This module's env fixture builds the meta DB only; the capture store
        # is a separate database and these tests are about what happens to it.
        main.ensure_starred_archive_schema()

    def _seed(self, *, tagged: bool, starred: bool, entry_id: str = "e1") -> str:
        with main.get_reader() as reader:
            reader.add_feed(FEED, allow_invalid_url=True, exist_ok=True)
            reader.add_entry({"feed_url": FEED, "id": entry_id, "title": "t", "link": entry_id})
            if tagged:
                entry = reader.get_entry((FEED, entry_id))
                reader.set_tag(entry.resource_id, "lectio.manual_tag.gamedev", "")
        if starred:
            with main.get_meta_connection() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id) VALUES (?, ?)",
                    (FEED, entry_id))
                conn.commit()
        with main.archive_conn() as ac:
            ac.execute("INSERT OR REPLACE INTO archived_entry (feed_url, entry_id, status, starred_at)"
                       " VALUES (?, ?, 'complete', 1.0)", (FEED, entry_id))
        return entry_id

    def _archive_rows(self) -> int:
        with main.archive_conn() as ac:
            return ac.execute(
                "SELECT COUNT(*) FROM archived_entry WHERE feed_url = ?", (FEED,)).fetchone()[0]

    def test_a_tagged_and_starred_entry_loses_everything(self, env):
        eid = self._seed(tagged=True, starred=True)
        counts = main.drop_all_curation(FEED)

        assert counts == {"untagged": 1, "unstarred": 1, "archives": 1}
        assert main.get_manual_tags_for_entry(FEED, eid) == []
        assert main._entry_is_starred(FEED, eid) is False
        assert self._archive_rows() == 0

    def test_a_tag_only_entry_loses_its_capture_too(self, env):
        """The case that motivated it: no star, so nothing else would release it."""
        eid = self._seed(tagged=True, starred=False)
        main.drop_all_curation(FEED)

        assert main.entry_has_keep_signal(FEED, eid, starred=False) is False
        assert self._archive_rows() == 0

    def test_nothing_survives_the_subsequent_purge(self, env, monkeypatch):
        """End to end: drop, then purge, and the orphan archive is not there."""
        monkeypatch.setattr(main, "websub_service", MagicMock())
        eid = self._seed(tagged=True, starred=True)
        main.drop_all_curation(FEED)
        with main.get_reader() as reader:
            with main.get_meta_connection() as conn:
                main.purge_orphaned_feed(reader, conn, FEED, archive_pending=False)
                conn.commit()

        assert self._archive_rows() == 0
        with main.get_meta_connection() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM orphan_entry_tags WHERE feed_url = ?", (FEED,)
            ).fetchone()[0] == 0
        assert main.entry_has_keep_signal(FEED, eid, starred=False) is False

    def test_another_feeds_curation_is_untouched(self, env):
        self._seed(tagged=True, starred=True)
        with main.get_reader() as reader:
            reader.add_feed(FEED2, allow_invalid_url=True, exist_ok=True)
            reader.add_entry({"feed_url": FEED2, "id": "keep-me", "title": "t", "link": "keep-me"})
            entry = reader.get_entry((FEED2, "keep-me"))
            reader.set_tag(entry.resource_id, "lectio.manual_tag.gamedev", "")

        main.drop_all_curation(FEED)
        assert main.get_manual_tags_for_entry(FEED2, "keep-me") == ["gamedev"]

    def test_an_uncurated_feed_is_a_noop(self, env):
        self._seed(tagged=False, starred=False, entry_id="plain")
        counts = main.drop_all_curation(FEED)
        assert counts["untagged"] == 0
        assert counts["unstarred"] == 0

    def test_route_runs_it_only_when_asked(self, env, monkeypatch):
        calls = []
        monkeypatch.setattr(main, "drop_all_curation",
                            lambda u: calls.append(u) or {"untagged": 0, "unstarred": 0, "archives": 0})
        monkeypatch.setattr(main, "websub_service", MagicMock())
        monkeypatch.setattr(main, "AUTH_ENABLED", False)

        _add_feed_to_folder(FEED, _root_folder_id())
        client, token = _csrf_client()
        client.post("/feeds/unsubscribe", data={
            "_csrf": token, "folder_id": _root_folder_id(), "feed_url": FEED,
        })
        assert calls == []

        _add_feed_to_folder(FEED, _root_folder_id())
        client.post("/feeds/unsubscribe", data={
            "_csrf": token, "folder_id": _root_folder_id(), "feed_url": FEED,
            "drop_curation": "1",
        })
        assert calls == [FEED]
