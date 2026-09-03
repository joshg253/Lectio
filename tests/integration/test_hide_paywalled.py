"""Per-feed 'hide paywalled' pref: the after-refresh pass auto-marks subscriber-only
stub entries read, and (Plan.md Tier 1 refresh-contention item) must batch its
meta-DB writes rather than committing once per stub entry."""
from __future__ import annotations

import datetime as dt

import pytest

import main
from services import tenancy

FEED = "https://example.com/feed.xml"
STUB_LINK_TMPL = "https://example.com/post-{i}"


def _reset_pools():
    main.close_thread_db_pools()


@pytest.fixture
def env(tmp_path):
    saved = tenancy._layout
    _reset_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    reader = main.get_reader()
    reader.add_feed(FEED, allow_invalid_url=True)
    try:
        yield
    finally:
        _reset_pools()
        with main._app_settings_cache_lock:
            main._app_settings_cache.clear()
        tenancy._layout = saved


def _add_stub_entry(entry_id: str):
    """An entry whose body is exactly a 'Read more' link back to itself --
    is_paywall_stub's definition of a subscriber-only stub."""
    link = STUB_LINK_TMPL.format(i=entry_id)
    reader = main.get_reader()
    reader.add_entry({
        "feed_url": FEED,
        "id": entry_id,
        "title": f"Stub {entry_id}",
        "link": link,
        "content": [{"value": f'<a href="{link}">Read more</a>', "type": "text/html"}],
        "published": dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
    })


def _read(entry_id):
    with main.get_reader() as reader:
        return reader.get_entry((FEED, entry_id)).read


def test_hide_paywalled_off_leaves_stubs_unread(env):
    _add_stub_entry("p1")
    main._apply_hide_paywalled({FEED})
    assert _read("p1") in (False, None)


def test_hide_paywalled_on_marks_stub_read(env):
    _add_stub_entry("p1")
    with main.get_meta_connection() as conn:
        main.upsert_feed_display_pref(conn, FEED, "hide_paywalled", 1)

    marked = main._apply_hide_paywalled({FEED})

    assert marked == 1
    assert _read("p1") is True
    with main.get_meta_connection() as conn:
        row = conn.execute(
            "SELECT read_at FROM entry_read_state WHERE feed_url = ? AND entry_id = ?",
            (FEED, "p1"),
        ).fetchone()
    assert row is not None


def test_hide_paywalled_batches_meta_writes(env, monkeypatch):
    """Multiple stub entries on the same feed must produce one batched
    executemany, not one commit per entry."""
    entry_count = 10
    for i in range(entry_count):
        _add_stub_entry(f"p{i}")
    with main.get_meta_connection() as conn:
        main.upsert_feed_display_pref(conn, FEED, "hide_paywalled", 1)

    connection_opens = 0
    real_get_meta_connection = main.get_meta_connection

    def counting_get_meta_connection():
        nonlocal connection_opens
        connection_opens += 1
        return real_get_meta_connection()

    monkeypatch.setattr(main, "get_meta_connection", counting_get_meta_connection)

    marked = main._apply_hide_paywalled({FEED})

    assert marked == entry_count
    for i in range(entry_count):
        assert _read(f"p{i}") is True
    # 1 to read the hide_paywalled targets + 1 to write the batch -- not one
    # meta-connection use per stub entry.
    assert connection_opens <= 2
