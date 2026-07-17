"""Save/Star automation rule: stars matching fresh entries into the Saved
backlog and tags them 'inbox'; plus the safe-dedup GUID signal that lets
identical-GUID cross-feed copies (slickdeals search feeds) dedupe without a
body match."""
from __future__ import annotations

import pytest
from fastapi import FastAPI  # noqa: F401 — parity with sibling test modules

import main
from services import tenancy

FEED = "https://deals.example.test/feed"


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


def _add_rule(keyword: str = "switch") -> None:
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO highlight_keywords (scope, scope_id, keyword, color, is_regex,"
            " enabled, type, search_in) VALUES ('feed', ?, ?, 'yellow', 0, 1, 'save_article', 'title')",
            (FEED, keyword),
        )


def _seed(reader) -> None:
    reader.add_feed(FEED, exist_ok=True)
    reader.add_entry({"feed_url": FEED, "id": "e-match",
                      "link": "https://deals.example.test/switch-oled-199",
                      "title": "Nintendo Switch OLED $199"})
    reader.add_entry({"feed_url": FEED, "id": "e-other",
                      "link": "https://deals.example.test/socks-3",
                      "title": "Wool socks 3-pack $9"})


def _is_starred(entry_id: str) -> bool:
    with main.get_meta_connection() as conn:
        return conn.execute(
            "SELECT 1 FROM saved_entries WHERE feed_url = ? AND entry_id = ?",
            (FEED, entry_id)).fetchone() is not None


def test_rule_stars_and_inbox_tags_matching_entries(configured):
    _add_rule("switch")
    with main.get_reader() as reader:
        _seed(reader)
    main._run_save_article_rules_after_refresh({FEED})

    assert _is_starred("e-match")
    assert not _is_starred("e-other")
    assert "inbox" in main.get_manual_tags_for_entry(FEED, "e-match")
    assert main.get_manual_tags_for_entry(FEED, "e-other") == []


def test_rerun_does_not_retag_filed_entries(configured):
    _add_rule("switch")
    with main.get_reader() as reader:
        _seed(reader)
    main._run_save_article_rules_after_refresh({FEED})
    # User files the item out of the Inbox (removes the tag, keeps the star).
    main.set_manual_tags_for_entry(FEED, "e-match", "toread")
    main._run_save_article_rules_after_refresh({FEED})
    assert main.get_manual_tags_for_entry(FEED, "e-match") == ["toread"]


def test_rule_ignores_entries_older_than_cutoff(configured, monkeypatch):
    _add_rule("switch")
    with main.get_reader() as reader:
        _seed(reader)
    # Pretend the refresh happened long after the entries arrived.
    from datetime import datetime, timedelta, timezone
    real_now = datetime.now

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return real_now(tz) + timedelta(hours=2)

    monkeypatch.setattr(main, "datetime", _FakeDateTime)
    main._run_save_article_rules_after_refresh({FEED})
    assert not _is_starred("e-match")


def test_run_now_dedup_sweeps_full_backlog(configured, monkeypatch):
    """User-triggered Run Now must not use the 500-per-feed refresh sample —
    older duplicates (e.g. entries restored to unread) live outside it."""
    from fastapi.testclient import TestClient

    seen = {}

    def fake_run_now_dedup(conn, scope, scope_id, match_method, window_hours,
                           max_per_feed=500, exclude_scope_ids=""):
        seen["max_per_feed"] = max_per_feed
        return {"count": 0, "entries": [], "kept": []}

    monkeypatch.setattr(main, "_run_now_dedup", fake_run_now_dedup)
    app = FastAPI()
    app.post("/rules/run-now")(main.rules_run_now_route)
    with TestClient(app) as c:
        r = c.post("/rules/run-now", data={"type": "deduplicate", "scope": "folder",
                                           "scope_id": "8", "keyword": "safe"})
    assert r.status_code == 200
    assert seen["max_per_feed"] == 10000


# ── safe-dedup GUID signal ────────────────────────────────────────────────────

def _rec(feed: str, entry_id: str, link: str, title: str = "", body: str = "") -> dict:
    return {"feed_url": feed, "entry_id": entry_id, "title": title, "link": link,
            "feed_title": feed, "published": None, "published_ts": 100.0,
            "slug": None, "ntitle": title.lower(), "body": body}


def test_safe_dedup_same_guid_cross_feed_is_sufficient():
    records = [
        _rec("https://a.test/f1", "https://slickdeals.net/thread-19718808", "https://a.test/f/1?src=1"),
        _rec("https://a.test/f2", "https://slickdeals.net/thread-19718808", "https://a.test/f/1?src=2"),
    ]
    pairs = main._safe_dedup_find_pairs(records)
    assert len(pairs) == 1
    assert list(pairs.values())[0] == ["guid"]


def test_safe_dedup_short_or_distinct_guids_do_not_match():
    # Degenerate short ids must not cross-match; title-only still rejected.
    records = [
        _rec("https://a.test/f1", "1", "https://a.test/x", title="Same Great Title Here"),
        _rec("https://a.test/f2", "1", "https://a.test/y", title="Same Great Title Here"),
    ]
    assert main._safe_dedup_find_pairs(records) == {}
