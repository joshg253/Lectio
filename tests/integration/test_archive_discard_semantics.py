"""POST /entries/archive and /entries/discard — the read-later triage pair.

Josh's model (2026-07-29): a feed post only needs read/unread, but a **star is a
TODO** ("I still have to decide what to do with this"), so saved items need a
second layer. Archive and Delete are the two ways to discharge it, and the point
of the pair is that you can act from the list without opening anything — like
clearing an email inbox by subject line.

    Archive = mark this To Read item as read, KEEP its contents.
    Delete  = I'm done; don't necessarily remove it now, but stop protecting it.

The tests here are mostly about what must *survive*, because every failure in
this area is silent and destructive: archiving is implemented as an unstar, and
the unstar path releases the offline capture and hard-deletes a Saved Articles
husk once nothing is keeping the entry.
"""
from __future__ import annotations

import pytest

import main
from services import tenancy
from services.saved_articles import SAVED_FEED_URL

FEED = "https://example.test/feed"
MTAG = main.MANUAL_TAG_KEY_PREFIX


@pytest.fixture
def configured(tmp_path, monkeypatch):
    saved = tenancy._layout
    main.close_thread_db_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()

    # Record capture-removal requests instead of running the archive worker.
    removals: list[tuple[str, str]] = []
    monkeypatch.setattr(main.starred_archive_service, "enqueue_removal",
                        lambda f, e: removals.append((f, e)))
    monkeypatch.setattr(main.starred_archive_service, "enqueue_archive", lambda f, e: None)

    with main.get_reader() as reader:
        for feed in (FEED, SAVED_FEED_URL):
            reader.add_feed(feed, allow_invalid_url=True, exist_ok=True)
            reader.disable_feed_updates(feed)
        # starred, untagged      — the plain TODO
        reader.add_entry({"feed_url": FEED, "id": "star", "link": "https://example.test/star"})
        # tagged, not starred    — could not be archived at all before
        reader.add_entry({"feed_url": FEED, "id": "tagged", "link": "https://example.test/tagged"})
        reader.set_tag((FEED, "tagged"), f"{MTAG}python")
        # a URL-saved article: nothing but the star holds it
        reader.add_entry({"feed_url": SAVED_FEED_URL, "id": "https://ex.test/a",
                          "link": "https://ex.test/a"})
    with main.get_meta_connection() as conn:
        conn.execute("INSERT INTO saved_entries (feed_url, entry_id) VALUES (?, ?)", (FEED, "star"))
        conn.execute("INSERT INTO saved_entries (feed_url, entry_id) VALUES (?, ?)",
                     (SAVED_FEED_URL, "https://ex.test/a"))
        conn.commit()
    try:
        yield removals
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _archive(feed: str, eid: str, on: bool = True):
    return main.toggle_entry_archived(None, feed_url=feed, entry_id=eid, archived=int(on))


def _starred(feed: str, eid: str) -> bool:
    with main.get_meta_connection() as conn:
        return conn.execute(
            "SELECT 1 FROM saved_entries WHERE feed_url = ? AND entry_id = ?", (feed, eid)
        ).fetchone() is not None


def _read(feed: str, eid: str) -> bool:
    with main.get_reader() as reader:
        return bool(reader.get_entry((feed, eid)).read)


# ── Archive ──
def test_archive_discharges_the_todo_and_marks_read(configured):
    _archive(FEED, "star")

    assert (FEED, "star") in main.get_archived_saved_keys()
    assert not _starred(FEED, "star")   # the TODO is discharged
    assert _read(FEED, "star")


def test_archive_marks_read_at_both_levels(configured):
    """reader's flag alone doesn't stick — entry_read_state is an override
    re-applied on refresh, so a miss here silently un-reads on the next poll."""
    _archive(FEED, "star")

    with main.get_meta_connection() as conn:
        assert conn.execute(
            "SELECT 1 FROM entry_read_state WHERE feed_url = ? AND entry_id = ?",
            (FEED, "star"),
        ).fetchone() is not None


def test_archive_lands_in_read_history(configured):
    """History is how an archived item is found again, so triage must record it."""
    _archive(FEED, "star")

    with main.get_meta_connection() as conn:
        assert conn.execute(
            "SELECT 1 FROM read_history WHERE feed_url = ? AND entry_id = ?", (FEED, "star")
        ).fetchone() is not None


def test_archive_keeps_the_offline_capture(configured):
    """"Keep its contents" is the whole definition of Archive.

    Archiving unstars, and the unstar path releases the capture once no keep
    signal remains — so Archive must itself count as one. Without that, the
    gesture that promises to keep the contents is the gesture that deletes them.
    """
    removals = configured
    _archive(FEED, "star")

    assert removals == []


def test_archiving_a_saved_article_does_not_hard_delete_it(configured):
    """The husk rule removes a `lectio:saved` entry once nothing keeps it.
    Archive keeps it, so the entry must survive — for a URL-saved article the
    capture is often the only copy of the content."""
    _archive(SAVED_FEED_URL, "https://ex.test/a")

    with main.get_reader() as reader:
        assert reader.get_entry((SAVED_FEED_URL, "https://ex.test/a"), None) is not None


def test_a_tag_kept_item_can_be_archived(configured):
    """The case that was impossible before, and the majority one on live data
    (16,479 tagged vs 10,002 starred)."""
    _archive(FEED, "tagged")

    assert (FEED, "tagged") in main.get_archived_saved_keys()
    assert main.get_manual_tags_for_entry(FEED, "tagged") == ["python"]


def test_unarchive_restores_the_todo(configured):
    _archive(FEED, "star")
    _archive(FEED, "star", on=False)

    assert (FEED, "star") not in main.get_archived_saved_keys()
    assert _starred(FEED, "star")   # back on the TODO pile


def test_unarchive_leaves_read_state_alone(configured):
    """"Read but not archived" is a real state — you read it and still haven't
    decided what to do with it. That combination is the reason the second axis
    exists, so un-archiving must not silently mark things unread."""
    _archive(FEED, "star")
    _archive(FEED, "star", on=False)

    assert _read(FEED, "star")


# ── Delete ──
def test_discard_drops_every_keep_signal_and_marks_read(configured):
    main.discard_entry(None, feed_url=FEED, entry_id="tagged")

    assert not _starred(FEED, "tagged")
    assert main.get_manual_tags_for_entry(FEED, "tagged") == []
    assert _read(FEED, "tagged")


def test_discard_releases_the_capture(configured):
    """The inverse of Archive: Delete explicitly does not need the contents."""
    removals = configured
    main.discard_entry(None, feed_url=FEED, entry_id="star")

    assert (FEED, "star") in removals


def test_discard_clears_tags_before_unstarring(configured):
    """Order is the reason this is a route and not a client-side chain.

    The capture is released only when no keep signal remains, so unstarring
    first leaves the tag holding the entry, the removal never fires, and the
    captured copy is stranded with nothing keeping it.
    """
    removals = configured
    main.discard_entry(None, feed_url=FEED, entry_id="tagged")

    assert (FEED, "tagged") in removals


def test_discard_does_not_delete_an_ordinary_feed_post(configured):
    """"Don't necessarily delete it now" — a feed post goes back to being a feed
    post and takes its chances with per-folder retention."""
    main.discard_entry(None, feed_url=FEED, entry_id="star")

    with main.get_reader() as reader:
        assert reader.get_entry((FEED, "star"), None) is not None


def test_discard_also_clears_the_archived_row(configured):
    """Delete must win over Archive, and forgetting this fails twice quietly.

    Reported live: an archived post was deleted — tag gone, star gone — and it
    still sat in the Archive list, because nothing cleared the done-axis row.
    """
    _archive(FEED, "star")
    main.discard_entry(None, feed_url=FEED, entry_id="star")

    assert (FEED, "star") not in main.get_archived_saved_keys()


def test_discarding_an_archived_item_still_releases_the_capture(configured):
    """The second, quieter half of the same bug.

    Archived counts as a keep signal, so leaving the row in place meant the
    unstar found one and skipped enqueue_removal — Delete kept the contents it
    exists to drop. Only visible by watching the removal queue, never in the UI.
    """
    removals = configured
    _archive(FEED, "star")
    assert removals == []          # archiving kept it, as it must

    main.discard_entry(None, feed_url=FEED, entry_id="star")
    assert (FEED, "star") in removals
