"""_apply_migration_items (Miniflux/FreshRSS/TTRSS import applier) and its
siblings (_run_import_loop, inoreader_import_json, _inoreader_drip_step) share
one bug class with OPML import (see test_opml_import_idempotency.py, commit
69e66af): they canonicalize the incoming feed URL, then compare/look it up
against a RAW, uncanonicalized reader.get_feeds() set. A pre-existing
subscription whose stored URL isn't already canonical (a trailing slash is
enough) doesn't match, so it gets a duplicate feed — and since the same raw
canonical URL is then used for entry lookups, a duplicate ENTRY too, because
the real one (stored under the old URL) is never found and gets
re-synthesized.

_canonical_feed_url_lookup / _resolve_feed_url are the shared fix; these tests
exercise them directly and through _apply_migration_items end to end."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

import main
from services import tenancy

FEED_CANONICAL = "https://example.test/feed"
FEED_STORED_NONCANONICAL = "https://example.test/feed/"  # trailing slash


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
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _item(**overrides) -> dict:
    base = {
        "url": "", "feed_url": FEED_CANONICAL, "title": "", "published": None,
        "feed_title": "", "content": "", "starred": False, "tags": [], "folder": "",
    }
    base.update(overrides)
    return base


def test_canonical_feed_url_lookup_resolves_noncanonical_stored_urls(configured):
    with main.get_reader() as reader:
        reader.add_feed(FEED_STORED_NONCANONICAL, exist_ok=True)
    with main.get_reader() as reader:
        lookup = main._canonical_feed_url_lookup(reader)
    assert lookup[FEED_CANONICAL] == FEED_STORED_NONCANONICAL
    assert main._resolve_feed_url(FEED_CANONICAL, lookup) == FEED_STORED_NONCANONICAL
    assert main._resolve_feed_url("https://new.test/feed", lookup) == "https://new.test/feed"


def test_migration_subscribe_does_not_duplicate_noncanonical_feed(configured):
    with main.get_reader() as reader:
        reader.add_feed(FEED_STORED_NONCANONICAL, exist_ok=True)

    state: dict = {}
    main._apply_migration_items([_item()], state, lambda: None)

    assert state.get("subs_added", 0) == 0
    with main.get_reader() as reader:
        urls = {str(f.url) for f in reader.get_feeds()}
    assert urls == {FEED_STORED_NONCANONICAL}


def test_migration_subscribes_a_genuinely_new_feed(configured):
    state: dict = {}
    main._apply_migration_items([_item(feed_url="https://new.test/feed")], state, lambda: None)

    assert state.get("subs_added") == 1
    with main.get_reader() as reader:
        urls = {str(f.url) for f in reader.get_feeds()}
    assert urls == {"https://new.test/feed"}


def test_migration_tags_land_on_existing_entry_not_a_synthesized_duplicate(configured):
    published = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with main.get_reader() as reader:
        reader.add_feed(FEED_STORED_NONCANONICAL, exist_ok=True)
        reader.add_entry({
            "feed_url": FEED_STORED_NONCANONICAL, "id": "https://example.test/a",
            "link": "https://example.test/a", "title": "A", "published": published,
        })

    state: dict = {}
    main._apply_migration_items(
        [_item(url="https://example.test/a", starred=True, tags=["keep"])],
        state, lambda: None,
    )

    assert state.get("items_tagged") == 1
    assert state.get("items_starred") == 1
    with main.get_reader() as reader:
        # Only the one real entry exists — nothing was synthesized under the
        # canonical feed URL as a duplicate.
        entries = list(reader.get_entries())
        feeds = {str(f.url) for f in reader.get_feeds()}
    assert len(entries) == 1
    assert str(entries[0].feed_url) == FEED_STORED_NONCANONICAL
    assert feeds == {FEED_STORED_NONCANONICAL}
    with main.get_meta_connection() as conn:
        row = conn.execute(
            "SELECT feed_url FROM saved_entries WHERE entry_id = 'https://example.test/a'"
        ).fetchone()
    assert row is not None
    assert row["feed_url"] == FEED_STORED_NONCANONICAL


# ---------------------------------------------------------------------------
# declined_feeds: an Ino resync must not resurrect a genuine unsubscribe.
# See Plan.md "Ino import resurrects deliberately-unsubscribed feeds" —
# _run_import_loop and _inoreader_drip_step's subscriptions phase both did
# reader.add_feed(furl, exist_ok=True) for anything Ino still lists as
# subscribed but reader is missing, with no check for *why* it's missing.
# ---------------------------------------------------------------------------

DECLINED_FEED = "https://declined.test/feed"
NEW_FEED = "https://new.test/never-declined"


def _decline(feed_url: str) -> None:
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO declined_feeds (feed_url, declined_at) VALUES (?, ?)",
            (feed_url, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def test_local_import_loop_skips_a_declined_feed(configured, tmp_path, monkeypatch):
    _decline(DECLINED_FEED)
    items = [
        {"url": "", "feed_url": DECLINED_FEED, "title": "", "published": None,
         "feed_title": "", "content": "", "starred": False, "tags": [], "folder": ""},
        {"url": "", "feed_url": NEW_FEED, "title": "", "published": None,
         "feed_title": "", "content": "", "starred": False, "tags": [], "folder": ""},
    ]
    monkeypatch.setattr(main.inoreader_service, "parse_export_json", lambda data: items)
    json_path = tmp_path / "export.json"
    json_path.write_text("{}", encoding="utf-8")

    state: dict = {"subs_added": 0, "items_tagged": 0, "items_starred": 0, "errors": 0}
    main._run_import_loop([json_path], state, lambda: None)

    with main.get_reader() as reader:
        urls = {str(f.url) for f in reader.get_feeds()}
    assert urls == {NEW_FEED}
    assert state.get("subs_added") == 1


def test_drip_step_subscriptions_phase_skips_a_declined_feed(configured, monkeypatch):
    _decline(DECLINED_FEED)
    subs = [{"feed_url": DECLINED_FEED}, {"feed_url": NEW_FEED}]
    monkeypatch.setattr(main, "get_inoreader_token", lambda: "fake-token")
    monkeypatch.setattr(main.inoreader_service, "get_subscriptions", lambda token: (subs, {}))

    with main.get_meta_connection() as conn:
        main.set_setting(
            conn, main.SETTING_INOREADER_IMPORT_STATE,
            json.dumps({"phase": "subscriptions"}),
        )

    main._inoreader_drip_step()

    with main.get_reader() as reader:
        urls = {str(f.url) for f in reader.get_feeds()}
    assert urls == {NEW_FEED}
    with main.get_meta_connection() as conn:
        state = json.loads(main.get_setting(conn, main.SETTING_INOREADER_IMPORT_STATE) or "{}")
    assert state.get("subs_added") == 1
    assert state.get("subs_declined_skipped") == 1


def test_drip_step_subscriptions_phase_re_adds_when_nothing_declined(configured, monkeypatch):
    """No declined_feeds rows at all must not change existing add behavior."""
    subs = [{"feed_url": NEW_FEED}]
    monkeypatch.setattr(main, "get_inoreader_token", lambda: "fake-token")
    monkeypatch.setattr(main.inoreader_service, "get_subscriptions", lambda token: (subs, {}))

    with main.get_meta_connection() as conn:
        main.set_setting(
            conn, main.SETTING_INOREADER_IMPORT_STATE,
            json.dumps({"phase": "subscriptions"}),
        )

    main._inoreader_drip_step()

    with main.get_reader() as reader:
        urls = {str(f.url) for f in reader.get_feeds()}
    assert urls == {NEW_FEED}


def test_drip_step_subscriptions_phase_places_new_feed_in_its_ino_folder(configured, monkeypatch):
    """A newly-added feed with a Title-Case (folder-shaped) category lands in
    that folder instead of Uncategorized. The lowercase category on the same
    subscription is an article tag, not a folder, and must be ignored."""
    subs = [{
        "feed_url": NEW_FEED,
        "categories": [
            {"id": "user/-/label/lessons"},
            {"id": "user/-/label/Comics & Art"},
        ],
    }]
    monkeypatch.setattr(main, "get_inoreader_token", lambda: "fake-token")
    monkeypatch.setattr(main.inoreader_service, "get_subscriptions", lambda token: (subs, {}))

    with main.get_meta_connection() as conn:
        main.set_setting(
            conn, main.SETTING_INOREADER_IMPORT_STATE,
            json.dumps({"phase": "subscriptions"}),
        )

    main._inoreader_drip_step()

    with main.get_meta_connection() as conn:
        row = conn.execute(
            "SELECT f.name FROM folder_feeds ff JOIN folders f ON f.id = ff.folder_id"
            " WHERE ff.feed_url = ?",
            (NEW_FEED,),
        ).fetchone()
    assert row is not None and row[0] == "Comics & Art"


def test_drip_step_subscriptions_phase_does_not_refolder_an_existing_feed(configured, monkeypatch):
    """Re-syncing a feed Lectio already has must not move it — Josh may have
    deliberately relocated it since the first import."""
    with main.get_reader() as reader:
        reader.add_feed(NEW_FEED, exist_ok=True)
    with main.get_meta_connection() as conn:
        other_folder_id = main._get_or_create_folder_by_name(conn, "Somewhere Else")
        conn.execute(
            "INSERT OR IGNORE INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)",
            (other_folder_id, NEW_FEED),
        )
        conn.commit()

    subs = [{"feed_url": NEW_FEED, "categories": [{"id": "user/-/label/Comics & Art"}]}]
    monkeypatch.setattr(main, "get_inoreader_token", lambda: "fake-token")
    monkeypatch.setattr(main.inoreader_service, "get_subscriptions", lambda token: (subs, {}))

    with main.get_meta_connection() as conn:
        main.set_setting(
            conn, main.SETTING_INOREADER_IMPORT_STATE,
            json.dumps({"phase": "subscriptions"}),
        )

    main._inoreader_drip_step()

    with main.get_meta_connection() as conn:
        row = conn.execute(
            "SELECT f.name FROM folder_feeds ff JOIN folders f ON f.id = ff.folder_id"
            " WHERE ff.feed_url = ?",
            (NEW_FEED,),
        ).fetchone()
    assert row is not None and row[0] == "Somewhere Else"
