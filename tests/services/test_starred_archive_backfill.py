"""`backfill_saved_entries_from_archive` restores stars from the archive DB, but
an archive row outlives its entry: filing a saved article into a real feed
hard-deletes the `lectio:saved` source and leaves its archive row behind. Before
the entry-existence check, every restart "restored" a star pointing at a
tombstone — invisible in the UI, but inflating counts and re-created on the next
boot however many times it was swept. These tests pin that the backfill restores
live entries only."""
from __future__ import annotations

import sqlite3

import pytest

from services.starred_archive import StarredArchiveService

SAVED_FEED = "lectio:saved"


class _FakeReader:
    """Stands in for the reader context manager; holds the (feed, id) it knows."""

    def __init__(self, known: set[tuple[str, str]], *, raises: bool = False) -> None:
        self._known = known
        self._raises = raises

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_entry(self, key, default=None):
        if self._raises:
            raise RuntimeError("reader exploded")
        return object() if key in self._known else default


@pytest.fixture
def dbs(tmp_path):
    archive = sqlite3.connect(str(tmp_path / "archive.sqlite"))
    archive.row_factory = sqlite3.Row
    archive.execute(
        """
        CREATE TABLE archived_entry (
            feed_url TEXT NOT NULL,
            entry_id TEXT NOT NULL,
            status TEXT NOT NULL,
            PRIMARY KEY (feed_url, entry_id)
        )
        """
    )
    meta = sqlite3.connect(str(tmp_path / "meta.sqlite3"))
    meta.execute(
        """
        CREATE TABLE saved_entries (
            feed_url TEXT NOT NULL,
            entry_id TEXT NOT NULL,
            saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            archived_at TIMESTAMP DEFAULT NULL,
            PRIMARY KEY (feed_url, entry_id)
        )
        """
    )
    meta.commit()
    archive.commit()
    yield archive, meta
    archive.close()
    meta.close()


def _service(archive, meta, reader, tagged=None, tagged_raises=False):
    # A fresh connection per call, as the app's factory does: the service
    # closes what it opens, so handing it the fixture's own handle would close
    # it out from under the test's assertions.
    archive_path = archive.execute("PRAGMA database_list").fetchone()[2]

    def get_archive_connection():
        conn = sqlite3.connect(archive_path)
        conn.row_factory = sqlite3.Row
        return conn

    def manually_tagged_keys():
        if tagged_raises:
            raise RuntimeError("reader db unavailable")
        return set(tagged or ())

    return StarredArchiveService(
        get_archive_connection=get_archive_connection,
        get_meta_connection=lambda: meta,
        get_reader=lambda: reader,
        user_agent="test",
        sanitize_readability_html=lambda h: h,
        manually_tagged_keys=manually_tagged_keys,
    )


def _add_archive(archive, entry_id, *, feed=SAVED_FEED, status="complete"):
    archive.execute(
        "INSERT INTO archived_entry (feed_url, entry_id, status) VALUES (?, ?, ?)",
        (feed, entry_id, status),
    )
    archive.commit()


def _stars(meta) -> set[tuple[str, str]]:
    return {(f, i) for f, i in meta.execute("SELECT feed_url, entry_id FROM saved_entries")}


def test_restores_star_for_an_entry_reader_still_has(dbs):
    archive, meta = dbs
    _add_archive(archive, "live-1")
    svc = _service(archive, meta, _FakeReader({(SAVED_FEED, "live-1")}))

    assert svc.backfill_saved_entries_from_archive() == 1
    assert _stars(meta) == {(SAVED_FEED, "live-1")}


def test_skips_archive_row_whose_entry_is_gone(dbs):
    """The orphaned-star-row bug: the article was filed into a real feed, so the
    lectio:saved entry is a tombstone and its star must not come back."""
    archive, meta = dbs
    _add_archive(archive, "filed-away")
    svc = _service(archive, meta, _FakeReader(set()))

    assert svc.backfill_saved_entries_from_archive() == 0
    assert _stars(meta) == set()


def test_restores_only_the_live_entries_in_a_mixed_batch(dbs):
    archive, meta = dbs
    for eid in ("live-1", "gone-1", "live-2", "gone-2"):
        _add_archive(archive, eid)
    svc = _service(
        archive, meta, _FakeReader({(SAVED_FEED, "live-1"), (SAVED_FEED, "live-2")})
    )

    assert svc.backfill_saved_entries_from_archive() == 2
    assert _stars(meta) == {(SAVED_FEED, "live-1"), (SAVED_FEED, "live-2")}


def test_pending_archive_rows_are_ignored(dbs):
    """Only `complete` rows are a restore signal, live entry or not."""
    archive, meta = dbs
    _add_archive(archive, "still-queued", status="pending")
    svc = _service(archive, meta, _FakeReader({(SAVED_FEED, "still-queued")}))

    assert svc.backfill_saved_entries_from_archive() == 0
    assert _stars(meta) == set()


def test_existing_star_is_left_alone(dbs):
    """INSERT OR IGNORE — a live star keeps its original saved_at rather than
    being restamped to now."""
    archive, meta = dbs
    _add_archive(archive, "already-starred")
    meta.execute(
        "INSERT INTO saved_entries (feed_url, entry_id, saved_at) VALUES (?, ?, ?)",
        (SAVED_FEED, "already-starred", "2019-12-08 10:00:00"),
    )
    meta.commit()
    svc = _service(archive, meta, _FakeReader({(SAVED_FEED, "already-starred")}))

    assert svc.backfill_saved_entries_from_archive() == 0
    saved_at = meta.execute(
        "SELECT saved_at FROM saved_entries WHERE entry_id = ?", ("already-starred",)
    ).fetchone()[0]
    assert saved_at == "2019-12-08 10:00:00"


def test_a_failing_reader_lookup_is_treated_as_missing(dbs):
    """Never resurrect a star on an inconclusive lookup — same conservative rule
    as the dupe scan's probe arming."""
    archive, meta = dbs
    _add_archive(archive, "unknown")
    svc = _service(archive, meta, _FakeReader(set(), raises=True))

    assert svc.backfill_saved_entries_from_archive() == 0
    assert _stars(meta) == set()


def test_a_manually_tagged_entry_is_never_starred(dbs):
    """Since tag-as-keep a tag archives too, so a complete archive row no longer
    implies the entry was starred. Retro-archiving tagged entries (Part C pass 1)
    used to convert them into stars at the next boot."""
    archive, meta = dbs
    _add_archive(archive, "tagged-only", feed="https://example.com/feed")
    svc = _service(
        archive,
        meta,
        _FakeReader({("https://example.com/feed", "tagged-only")}),
        tagged={("https://example.com/feed", "tagged-only")},
    )

    assert svc.backfill_saved_entries_from_archive() == 0
    assert _stars(meta) == set()


def test_tagged_and_untagged_are_separated_in_one_batch(dbs):
    archive, meta = dbs
    _add_archive(archive, "starred-only", feed="https://example.com/feed")
    _add_archive(archive, "tagged-only", feed="https://example.com/feed")
    live = {
        ("https://example.com/feed", "starred-only"),
        ("https://example.com/feed", "tagged-only"),
    }
    svc = _service(
        archive, meta, _FakeReader(live), tagged={("https://example.com/feed", "tagged-only")}
    )

    assert svc.backfill_saved_entries_from_archive() == 1
    assert _stars(meta) == {("https://example.com/feed", "starred-only")}


def test_a_failing_tag_lookup_restores_nothing(dbs):
    """Without the tag set every tagged entry would be starred, so bail entirely
    rather than guess — inventing stars is worse than skipping the recovery."""
    archive, meta = dbs
    _add_archive(archive, "e1", feed="https://example.com/feed")
    svc = _service(
        archive,
        meta,
        _FakeReader({("https://example.com/feed", "e1")}),
        tagged_raises=True,
    )

    assert svc.backfill_saved_entries_from_archive() == 0
    assert _stars(meta) == set()


def test_real_feed_rows_are_restored_too(dbs):
    """The recovery path this function exists for (meta DB reset) is not
    saved-feed specific and must keep working."""
    archive, meta = dbs
    _add_archive(archive, "e1", feed="https://example.com/feed")
    svc = _service(archive, meta, _FakeReader({("https://example.com/feed", "e1")}))

    assert svc.backfill_saved_entries_from_archive() == 1
    assert _stars(meta) == {("https://example.com/feed", "e1")}
