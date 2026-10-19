"""The scraped / dev.to feed refresh loops share one meta connection across a round of network fetches. Python's sqlite3 opens an implicit
transaction at the first write and keeps it until commit, so the whole round held the write lock and any other writer waited it out
(a tag-chip click took 8s while a round of scrapes ran). Each loop now commits between feeds."""

from __future__ import annotations

import sqlite3

import pytest

from services import devto, scraper_service


@pytest.fixture
def two_conns(tmp_path):
    path = str(tmp_path / "meta.sqlite")
    main_conn = sqlite3.connect(path, timeout=0.2)
    main_conn.row_factory = sqlite3.Row
    main_conn.execute("PRAGMA journal_mode=WAL")
    main_conn.execute("CREATE TABLE scraped_feeds (id TEXT PRIMARY KEY)")
    main_conn.execute("CREATE TABLE devto_feeds (id TEXT PRIMARY KEY, last_synced_at TEXT, enabled INTEGER DEFAULT 1)")
    main_conn.execute("CREATE TABLE scratch (v INTEGER)")
    for table in ("scraped_feeds", "devto_feeds"):
        main_conn.executemany(f"INSERT INTO {table} (id) VALUES (?)", [("a",), ("b",)])  # noqa: S608
    main_conn.commit()
    other = sqlite3.connect(path, timeout=0.2)
    try:
        yield main_conn, other
    finally:
        main_conn.close()
        other.close()


def _record_other_writer_outcomes(main_conn, other, outcomes):
    def fake_refresh(conn, feed_id):
        try:
            other.execute("INSERT INTO scratch (v) VALUES (1)")
            other.commit()
            outcomes.append("ok")
        except sqlite3.OperationalError:
            outcomes.append("locked")
        conn.execute("INSERT INTO scratch (v) VALUES (2)")  # this feed's own write opens the implicit transaction

    return fake_refresh


def test_scraped_feed_round_releases_the_write_lock_between_feeds(two_conns, monkeypatch):
    main_conn, other = two_conns
    outcomes: list[str] = []
    monkeypatch.setattr(scraper_service, "refresh_scraped_feed_by_id", _record_other_writer_outcomes(main_conn, other, outcomes))

    scraper_service.refresh_all_scraped_feeds(main_conn)

    assert outcomes == ["ok", "ok"]


def test_devto_round_releases_the_write_lock_between_feeds(two_conns, monkeypatch):
    main_conn, other = two_conns
    outcomes: list[str] = []
    monkeypatch.setattr(devto, "refresh_devto_feed_by_id", _record_other_writer_outcomes(main_conn, other, outcomes))

    devto.refresh_all_devto_feeds(main_conn)

    assert outcomes == ["ok", "ok"]


def test_a_round_does_not_inherit_a_transaction_the_caller_left_open(two_conns, monkeypatch):
    """The scheduler writes app_settings on the same pooled connection just before the rounds start; the first feed's fetch must not run
    under that implicit transaction."""
    main_conn, other = two_conns
    main_conn.execute("INSERT INTO scratch (v) VALUES (0)")  # opens the implicit transaction, uncommitted
    outcomes: list[str] = []
    monkeypatch.setattr(scraper_service, "refresh_scraped_feed_by_id", _record_other_writer_outcomes(main_conn, other, outcomes))

    scraper_service.refresh_all_scraped_feeds(main_conn)

    assert outcomes == ["ok", "ok"]


def test_feed_creation_scrape_stays_one_rollbackable_unit(two_conns, monkeypatch):
    """create_scraped_feed seeds the first scrape inside its own transaction; releasing the lock there would commit a half-created feed."""
    main_conn, _other = two_conns
    main_conn.execute(
        "CREATE TABLE scraped_entries "
        "(id TEXT, scraped_feed_id TEXT, entry_url TEXT, title TEXT, content TEXT, published_at TEXT, hidden INTEGER)"
    )
    main_conn.execute("ALTER TABLE scraped_feeds ADD COLUMN source_url TEXT")
    main_conn.execute("ALTER TABLE scraped_feeds ADD COLUMN last_scraped_at TEXT")
    main_conn.commit()
    monkeypatch.setattr(scraper_service, "_fetch_html", lambda url: '<a href="https://x.test/p1">Post one title</a>')
    monkeypatch.setattr(scraper_service, "_new_entry_extras", lambda url, sel: (None, ""))
    main_conn.execute("INSERT INTO scraped_feeds (id, source_url) VALUES ('new', 'https://x.test/')")

    scraper_service._scrape_link_list(
        main_conn, {"id": "new", "source_url": "https://x.test/", "selector": ""}, initial=True, release_lock=False
    )
    main_conn.rollback()

    assert main_conn.execute("SELECT COUNT(*) FROM scraped_entries").fetchone()[0] == 0
    assert main_conn.execute("SELECT COUNT(*) FROM scraped_feeds WHERE id = 'new'").fetchone()[0] == 0
