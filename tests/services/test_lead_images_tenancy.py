"""The background source-image fetch (`queue_source_fetch`) is spawned from a
render request but runs in a bare thread, which does not inherit the request's
tenancy contextvar. It must capture and re-bind the user, or the resolved image
is persisted to the default tenant's meta DB instead of the requesting user's —
the bug that left real users' lead images accumulating under DEFAULT_USER_ID."""
from __future__ import annotations

import sqlite3

import pytest

from services import tenancy
from services.lead_images import LeadImageService


class _FakeEntry:
    def __init__(self, feed_url, entry_id, link):
        self.feed_url = feed_url
        self.id = entry_id
        self.link = link
        self.read = False
        self.title = ""


class _ReaderCtx:
    def __init__(self, entries):
        self._entries = entries

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_entries(self, feed=None):
        return [e for e in self._entries if e.feed_url == feed]


def _make_meta(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS entry_lead_images (
            feed_url TEXT NOT NULL, entry_id TEXT NOT NULL, image_url TEXT,
            image_alt TEXT, image_title TEXT, fetched_at REAL,
            thumb_crop TEXT,
            PRIMARY KEY(feed_url, entry_id)
        )
        """
    )
    conn.commit()
    return conn


@pytest.fixture
def configured(tmp_path):
    saved = tenancy._layout
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "lectio_reader.sqlite",
        legacy_meta=tmp_path / "lectio_meta.sqlite3",
        legacy_starred=tmp_path / "lectio_starred_archive.sqlite",
    )
    # Pre-create every meta DB so reads/writes never auto-create at a wrong path.
    for p in (
        tenancy.meta_db_path(tenancy.DEFAULT_USER_ID),
        tenancy.meta_db_path("alice"),
    ):
        _make_meta(p).close()
    try:
        yield tmp_path
    finally:
        tenancy._layout = saved


def _rows(path):
    conn = _make_meta(path)
    try:
        return conn.execute("SELECT feed_url, entry_id, image_url FROM entry_lead_images").fetchall()
    finally:
        conn.close()


def test_queued_source_fetch_persists_under_the_requesting_user(configured):
    def get_meta_connection():
        # Resolves by current tenancy context — exactly like main.get_meta_connection.
        return _make_meta(tenancy.meta_db_path())

    svc = LeadImageService(
        get_meta_connection=get_meta_connection,
        get_reader=lambda: _ReaderCtx([]),
        user_agent="test",
        extract_video_id=lambda link: None,
    )
    svc._fetch_source_lead_image = lambda link, **kw: "https://cdn.example/hero.jpg"  # type: ignore[method-assign]
    svc._maybe_store_alt_from_cache = lambda *a, **kw: None  # type: ignore[method-assign]

    feed, entry = "https://alice.example/feed", "e1"
    with tenancy.user_context("alice"):
        svc.queue_source_fetch(feed, entry, "https://alice.example/post")
        assert svc.wait_for_source_fetch(feed, entry, timeout=5.0)

    # The image landed in alice's DB...
    alice_rows = _rows(tenancy.meta_db_path("alice"))
    assert [(r["feed_url"], r["entry_id"], r["image_url"]) for r in alice_rows] == [
        (feed, entry, "https://cdn.example/hero.jpg")
    ]
    # ...and NOT in the default tenant's DB (the regression).
    assert _rows(tenancy.meta_db_path(tenancy.DEFAULT_USER_ID)) == []


def test_queued_source_fetch_does_not_persist_a_none_result(configured):
    """A None from the interactive on-open source fetch is ambiguous — a transient
    network failure is indistinguishable from a genuine "no image". Persisting it
    would cement a momentary miss as a permanent negative and blank a thumbnail the
    feed actually has (regression: Standard Ebooks covers vanished after opening an
    entry whose page fetch transiently failed). The entry must stay unresolved so
    the background backfill and the next open can still recover the image."""
    def get_meta_connection():
        return _make_meta(tenancy.meta_db_path())

    svc = LeadImageService(
        get_meta_connection=get_meta_connection,
        get_reader=lambda: _ReaderCtx([]),
        user_agent="test",
        extract_video_id=lambda link: None,
    )
    svc._fetch_source_lead_image = lambda link, **kw: None  # type: ignore[method-assign]
    svc._maybe_store_alt_from_cache = lambda *a, **kw: None  # type: ignore[method-assign]

    feed, entry = "https://alice.example/feed", "e1"
    with tenancy.user_context("alice"):
        svc.queue_source_fetch(feed, entry, "https://alice.example/post")
        assert svc.wait_for_source_fetch(feed, entry, timeout=5.0)

    # Nothing persisted — the entry stays absent rather than a stored negative.
    assert _rows(tenancy.meta_db_path("alice")) == []
    assert (feed, entry) not in svc._cache


def test_chunk_backfill_persists_under_the_active_user(configured):
    """The chunk-level visible-entry backfill (backfill_entry_list) must persist
    under whatever tenancy user is active when it runs.  The home route spawns it
    in a bare daemon thread, so the caller must re-bind the user (regression:
    delightlylinux thumbnails not sticking across refreshes for the real user)."""
    def get_meta_connection():
        return _make_meta(tenancy.meta_db_path())

    svc = LeadImageService(
        get_meta_connection=get_meta_connection,
        get_reader=lambda: _ReaderCtx([]),
        user_agent="test",
        extract_video_id=lambda link: None,
    )
    svc._fetch_source_lead_image = lambda link, **kw: "https://cdn.example/hero.jpg"  # type: ignore[method-assign]
    svc._fetch_feed_media_thumbnails = lambda feed_url: {}  # type: ignore[method-assign]
    svc._maybe_store_alt_from_cache = lambda *a, **kw: None  # type: ignore[method-assign]

    feed, entry = "https://alice.example/feed", "e1"
    posts = [{"feed_url": feed, "id": entry, "link": "https://alice.example/post"}]
    with tenancy.user_context("alice"):
        svc.backfill_entry_list(posts)

    alice_rows = _rows(tenancy.meta_db_path("alice"))
    assert [(r["feed_url"], r["entry_id"], r["image_url"]) for r in alice_rows] == [
        (feed, entry, "https://cdn.example/hero.jpg")
    ]
    assert _rows(tenancy.meta_db_path(tenancy.DEFAULT_USER_ID)) == []
