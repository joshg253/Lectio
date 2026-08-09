"""Combining feeds carries the offline captures, not just stars and tags.

`_migrate_curation` moved curation onto the survivor but left the source feed's
starred-archive rows keyed to a feed that was about to be deleted. The captures
survived but became unreachable, and the Saved view rendered them as
archive-only *orphans* — from the archive row's own stale link, which is how it
surfaced: a combined feed's articles still showing their old, dead URLs.
Measured on the live library 2026-07-25, past combines had stranded 85 of them.
"""
from __future__ import annotations

import pytest

import main
from services import tenancy

OLD = "https://sadh.life/rss"
NEW = "https://tush.ar/rss.xml"
SHARED_ID = "https://tush.ar/post/dunders/"


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
    with main.get_reader() as reader:
        reader.add_feed(OLD, exist_ok=True)
        reader.add_feed(NEW, exist_ok=True)
        for feed in (OLD, NEW):
            reader.add_entry({
                "feed_url": feed, "id": SHARED_ID, "link": SHARED_ID, "title": "dunders",
            })
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _put_archive(feed_url: str, entry_id: str, body: bytes) -> None:
    with main.get_starred_archive_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO archived_entry"
            " (feed_url, entry_id, status, starred_at, content_html_zlib, title, link)"
            " VALUES (?, ?, 'complete', 0, ?, 'dunders', ?)",
            (feed_url, entry_id, body, entry_id),
        )
        conn.commit()


def _archive_feeds(entry_id: str) -> list[str]:
    with main.get_starred_archive_connection() as conn:
        return [r[0] for r in conn.execute(
            "SELECT feed_url FROM archived_entry WHERE entry_id = ?", (entry_id,)
        )]


def test_combine_carries_the_capture_to_the_survivor(configured):
    _put_archive(OLD, SHARED_ID, b"captured-page")
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO saved_entries (feed_url, entry_id, saved_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (OLD, SHARED_ID),
        )
        conn.commit()
        with main.get_reader() as reader:
            counts = main._migrate_curation(reader, conn, OLD, NEW)

    assert counts["stars"] == 1
    assert counts["archives"] == 1
    assert _archive_feeds(SHARED_ID) == [NEW], "the capture must follow the star onto the survivor"


def test_combine_does_not_clobber_an_existing_capture(configured):
    """rekey_archive refuses to overwrite a capture the survivor already has;
    the redundant source row is dropped rather than duplicated."""
    _put_archive(OLD, SHARED_ID, b"thin")
    _put_archive(NEW, SHARED_ID, b"the-survivors-own-richer-capture")
    with main.get_meta_connection() as conn:
        with main.get_reader() as reader:
            main._migrate_curation(reader, conn, OLD, NEW)

    assert _archive_feeds(SHARED_ID) == [NEW]
    with main.get_starred_archive_connection() as conn:
        body = conn.execute(
            "SELECT content_html_zlib FROM archived_entry WHERE feed_url = ? AND entry_id = ?",
            (NEW, SHARED_ID),
        ).fetchone()[0]
    assert body == b"the-survivors-own-richer-capture"


def test_combine_with_no_archive_is_still_fine(configured):
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO saved_entries (feed_url, entry_id, saved_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (OLD, SHARED_ID),
        )
        conn.commit()
        with main.get_reader() as reader:
            counts = main._migrate_curation(reader, conn, OLD, NEW)
    assert counts["stars"] == 1 and counts["archives"] == 0


UNCURATED = "https://tush.ar/post/only-on-the-old-feed/"


def test_an_uncurated_unread_post_moves_to_the_survivor(configured):
    """The reported loss: combining the two Sarah's Scribbles Webtoons feeds
    dropped the removed feed's single post because it carried no tag, no star
    and no capture — `_migrate_curation` walked curation, not entries, so it was
    never matched and never synthesized."""
    with main.get_reader() as reader:
        reader.add_entry({
            "feed_url": OLD, "id": UNCURATED, "link": UNCURATED,
            "title": "only on the old feed",
        })
        with main.get_meta_connection() as conn:
            counts = main._migrate_curation(reader, conn, OLD, NEW)

        survivors = {e.id: e for e in reader.get_entries(feed=NEW)}

    assert UNCURATED in survivors, "an uncurated post must still move"
    assert survivors[UNCURATED].title == "only on the old feed"
    assert survivors[UNCURATED].read is False, "an unread post arrives unread"
    assert counts["synth"] >= 1


def test_read_state_is_carried_rather_than_reset(configured):
    """Otherwise combining an old feed dumps its whole history into unread."""
    read_id = "https://tush.ar/post/already-read/"
    with main.get_reader() as reader:
        reader.add_entry({"feed_url": OLD, "id": read_id, "link": read_id, "title": "read one"})
        reader.mark_entry_as_read((OLD, read_id))
        with main.get_meta_connection() as conn:
            main._migrate_curation(reader, conn, OLD, NEW)
        moved = reader.get_entry((NEW, read_id))

    assert moved.read is True


def test_a_read_twin_on_the_survivor_is_not_resurrected_as_unread(configured):
    """SHARED_ID exists on both feeds. If the survivor's copy is already read,
    an unread source copy must not drag it back into the unread list."""
    with main.get_reader() as reader:
        reader.mark_entry_as_read((NEW, SHARED_ID))
        reader.mark_entry_as_unread((OLD, SHARED_ID))
        with main.get_meta_connection() as conn:
            main._migrate_curation(reader, conn, OLD, NEW)
        assert reader.get_entry((NEW, SHARED_ID)).read is True


def test_per_entry_meta_follows_the_entry(configured):
    """A moved post arriving with no thumbnail and its hand-made title
    correction reverted is a worse outcome than not moving it."""
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO entry_lead_images (feed_url, entry_id, image_url, fetched_at)"
            " VALUES (?, ?, ?, ?)",
            (OLD, UNCURATED, "https://cdn.test/panel.jpg", 1786232800.0),
        )
        conn.execute(
            "INSERT INTO entry_title_overrides (feed_url, entry_id, title) VALUES (?, ?, ?)",
            (OLD, UNCURATED, "corrected title"),
        )
        conn.commit()
    with main.get_reader() as reader:
        reader.add_entry({
            "feed_url": OLD, "id": UNCURATED, "link": UNCURATED, "title": "t",
        })
        with main.get_meta_connection() as conn:
            main._migrate_curation(reader, conn, OLD, NEW)

    with main.get_meta_connection() as conn:
        img = conn.execute(
            "SELECT image_url FROM entry_lead_images WHERE feed_url = ? AND entry_id = ?",
            (NEW, UNCURATED),
        ).fetchone()
        title = conn.execute(
            "SELECT title FROM entry_title_overrides WHERE feed_url = ? AND entry_id = ?",
            (NEW, UNCURATED),
        ).fetchone()
        left = conn.execute(
            "SELECT COUNT(*) FROM entry_lead_images WHERE feed_url = ?", (OLD,)
        ).fetchone()[0]

    assert img is not None and img[0] == "https://cdn.test/panel.jpg"
    assert title is not None and title[0] == "corrected title"
    assert left == 0, "the source rows are the leak this also closes"


def test_rekeying_read_history_does_not_lose_the_row(configured):
    """read_history has an INTEGER PRIMARY KEY. Copying it verbatim collides
    with the row being copied from, so INSERT OR IGNORE drops the copy and the
    DELETE then loses the row outright."""
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO read_history (feed_url, entry_id, title, link, feed_title, read_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (OLD, UNCURATED, "t", UNCURATED, "old feed", "2026-08-08T00:00:00"),
        )
        conn.commit()
        main._rekey_entry_meta(conn, OLD, UNCURATED, NEW, UNCURATED)
        conn.commit()
        rows = conn.execute(
            "SELECT feed_url FROM read_history WHERE entry_id = ?", (UNCURATED,)
        ).fetchall()

    assert [r[0] for r in rows] == [NEW]
