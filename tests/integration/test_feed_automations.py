"""The Feed Properties → Automations tab shows which configured rules act on a
feed (scoped to it, its folder, or all feeds) and the recent runs that touched
its entries. collect_feed_automations builds that data."""
from __future__ import annotations

import pytest

import main
from services import tenancy

FEED = "https://example.test/feed"
OTHER_FEED = "https://other.test/feed"


def _reset_pools():
    main.close_thread_db_pools()


@pytest.fixture
def meta(tmp_path):
    saved = tenancy._layout
    _reset_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    try:
        yield main.get_meta_connection()
    finally:
        _reset_pools()
        tenancy._layout = saved


def test_rules_filtered_by_scope(meta):
    # global + this-feed + this-feed's-folder apply; other-feed and other-folder do not.
    main.add_highlight_keyword(meta, "global", "", "breaking", "yellow", rule_type="highlight", enabled=1)
    main.add_highlight_keyword(meta, "feed", FEED, "spoiler", "red", rule_type="mark_as_read", enabled=1)
    main.add_highlight_keyword(meta, "feed", OTHER_FEED, "nope", "red", rule_type="mark_as_read", enabled=1)
    main.add_highlight_keyword(meta, "folder", "7", "dupes", "blue", rule_type="deduplicate", enabled=1)
    main.add_highlight_keyword(meta, "folder", "99", "elsewhere", "blue", rule_type="deduplicate", enabled=1)

    result = main.collect_feed_automations(meta, FEED, folder_ids=[7])
    keywords = {r["keyword"] for r in result["rules"]}

    assert keywords == {"breaking", "spoiler", "dupes"}


def test_disabled_rule_marked_not_dropped(meta):
    main.add_highlight_keyword(meta, "feed", FEED, "muted", "yellow", rule_type="highlight", enabled=0)

    rules = main.collect_feed_automations(meta, FEED, folder_ids=[])["rules"]

    assert len(rules) == 1
    assert rules[0]["enabled"] is False
    assert rules[0]["type_label"] == "Highlight"


def test_recent_runs_only_count_this_feed(meta):
    meta.execute(
        "INSERT INTO rule_run_log (run_at, rule_type, scope, scope_id, keyword, entries_affected)"
        " VALUES ('2026-06-17T10:00:00', 'mark_as_read', 'global', '', 'spoiler', 3)"
    )
    log_id = meta.execute("SELECT id FROM rule_run_log").fetchone()["id"]
    # Two entries on this feed, one on another feed under the same run.
    for eid, furl in [("e1", FEED), ("e2", FEED), ("e3", OTHER_FEED)]:
        meta.execute(
            "INSERT INTO rule_run_log_entries (log_id, feed_url, entry_id) VALUES (?, ?, ?)",
            (log_id, furl, eid),
        )
    meta.commit()

    runs = main.collect_feed_automations(meta, FEED, folder_ids=[])["recent_runs"]

    assert len(runs) == 1
    assert runs[0]["affected"] == 2
    assert runs[0]["type_label"] == "Auto mark read"
    assert runs[0]["keyword"] == "spoiler"


def test_feed_with_no_automations_is_empty(meta):
    result = main.collect_feed_automations(meta, FEED, folder_ids=[])
    assert result == {"rules": [], "recent_runs": []}


def test_feeds_scope_dedup_resolves_selected_feeds(meta):
    # Dedupe across an explicit set of feeds (scope "feeds"): the resolver returns
    # exactly the selected URLs without needing a folder.
    urls = main._resolve_dedup_feed_urls(meta, "feeds", f"{FEED}\n{OTHER_FEED}")
    assert urls == {FEED, OTHER_FEED}
    # A single feed still can't cross-dedupe.
    single = main._resolve_dedup_feed_urls(meta, "feed", FEED)
    assert isinstance(single, dict) and "error" in single


def test_feeds_scoped_rule_applies_to_member_feed(meta):
    main.add_highlight_keyword(meta, "feeds", f"{FEED}\n{OTHER_FEED}", "x", "yellow",
                               rule_type="highlight", enabled=1)
    applies = main.collect_feed_automations(meta, FEED, folder_ids=[])["rules"]
    assert any(r["scope_label"] == "Selected feeds" for r in applies)
    # A feed not in the set sees no rule.
    none = main.collect_feed_automations(meta, "https://nope.test/feed", folder_ids=[])["rules"]
    assert none == []


def test_youtube_playlist_rule_detail_and_label(meta):
    main.add_highlight_keyword(meta, "feed", FEED, "", "yellow", rule_type="youtube_playlist",
                               enabled=1, yt_playlist_id="PL1", yt_playlist_title="TV Queue",
                               yt_include_shorts=False, yt_mark_read=True, yt_min_minutes=60)
    rules = main.collect_feed_automations(meta, FEED, folder_ids=[])["rules"]
    r = next(x for x in rules if x["type"] == "youtube_playlist")
    assert r["type_label"] == "Add to YT playlist"
    assert "TV Queue" in r["detail"]
    assert "≥60m" in r["detail"] and "mark read" in r["detail"]
