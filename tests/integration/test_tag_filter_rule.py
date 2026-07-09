"""tag_filter rule: suppress (mark read) unread entries by their feed-provided
tags in entry_feed_tags. The rule spec lives in `keyword` as one comma-separated
field — `+tag` (or bare) keeps only matching tagged entries, `-tag` drops
matches; an include match saves the entry outright; untagged entries are
always kept."""
from __future__ import annotations

import datetime as dt

import pytest

import main
from services import tenancy

FEED = "https://example.test/feed"
OTHER_FEED = "https://other.test/feed"


def _reset_pools():
    main._reader_thread_local.pool = None
    main._meta_conn_local.pool = None


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
    when = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
    for eid, title in [("e-linux", "Linux post"), ("e-win", "Windows post"),
                       ("e-deal", "Deals post"), ("e-untagged", "No tags"),
                       ("e-mixed", "Linux deal")]:
        reader.add_entry({"feed_url": FEED, "id": eid, "title": title,
                          "link": f"https://example.test/{eid}", "summary": "x",
                          "published": when})
    # Raw (unnormalized) feed tags, as the ingest sink stores them.
    main.feed_tag_service.record_entry_tags(FEED, [
        ("e-linux", ["Linux"]),
        ("e-win", ["Windows 11"]),
        ("e-deal", ["Deals"]),
        ("e-mixed", ["Linux", "Deals"]),
    ])
    try:
        yield
    finally:
        _reset_pools()
        tenancy._layout = saved


def _unread_ids() -> set[str]:
    reader = main.get_reader()
    return {str(e.id) for e in reader.get_entries(feed=FEED, read=False)}


def test_parse_tag_filter_spec():
    # Comma-separated; leading - = exclude, leading + (or bare) = include;
    # tokens are normalized, so multi-word tags can be typed with spaces.
    assert main.parse_tag_filter_spec("+python, -rust") == ({"python"}, {"rust"})
    assert main.parse_tag_filter_spec("Linux, #AI, -Windows 11") == (
        {"linux", "ai"}, {"windows-11"}
    )
    assert main.parse_tag_filter_spec("  , #, -") == (set(), set())
    assert main.parse_tag_filter_spec(None) == (set(), set())


def test_rule_persists_spec(env):
    with main.get_meta_connection() as conn:
        main.add_highlight_keyword(conn, "feed", FEED, "+linux, -deals", "yellow",
                                   rule_type="tag_filter", enabled=1)
        r = main.get_highlight_keywords(conn)[0]
    assert r["type"] == "tag_filter"
    assert r["keyword"] == "+linux, -deals"


def test_exclude_only_rule(env):
    with main.get_meta_connection() as conn:
        result = main._run_tag_filter(conn, "feed", FEED, "-deals")
    assert result["count"] == 2  # e-deal + e-mixed
    assert _unread_ids() == {"e-linux", "e-win", "e-untagged"}


def test_include_list_keeps_untagged(env):
    with main.get_meta_connection() as conn:
        result = main._run_tag_filter(conn, "feed", FEED, "+linux")
    # e-win and e-deal are tagged but match no include tag → suppressed;
    # e-untagged has no tags → kept; e-linux and e-mixed match → kept.
    assert result["count"] == 2
    assert _unread_ids() == {"e-linux", "e-mixed", "e-untagged"}


def test_include_wins_over_exclude(env):
    with main.get_meta_connection() as conn:
        result = main._run_tag_filter(conn, "feed", FEED, "linux, -deals")
    # e-mixed matches include (linux) AND exclude (deals) → the include match
    # saves it; e-deal (deals only) and e-win (no include match) are cut.
    assert result["count"] == 2  # e-win, e-deal
    assert _unread_ids() == {"e-linux", "e-mixed", "e-untagged"}


def test_tags_normalized_both_sides(env):
    with main.get_meta_connection() as conn:
        result = main._run_tag_filter(conn, "feed", FEED, "-Windows 11")
    # The stored raw tag "Windows 11" and the spec token both normalize to
    # windows-11 — the match happens on normalized values.
    assert result["count"] == 1
    assert "e-win" not in _unread_ids()


def test_dry_run_marks_nothing_and_returns_preview_shape(env):
    with main.get_meta_connection() as conn:
        result = main._run_tag_filter(conn, "feed", FEED, "-deals", apply=False)
    # Dry-run returns the Test-panel shape: matches/total_scanned/total_matches.
    assert result["total_matches"] == 2
    assert result["total_scanned"] == 5
    assert {e["entry_id"] for e in result["matches"]} == {"e-deal", "e-mixed"}
    assert all("read" in e and "published" in e for e in result["matches"])
    assert len(_unread_ids()) == 5  # nothing marked


def test_dry_run_includes_read_entries(env):
    reader = main.get_reader()
    reader.mark_entry_as_read((FEED, "e-deal"))
    with main.get_meta_connection() as conn:
        result = main._run_tag_filter(conn, "feed", FEED, "-deals", apply=False)
    # The already-read e-deal still shows in the preview, flagged read.
    assert result["total_matches"] == 2
    flags = {e["entry_id"]: e["read"] for e in result["matches"]}
    assert flags == {"e-deal": True, "e-mixed": False}


def test_empty_lists_error(env):
    with main.get_meta_connection() as conn:
        result = main._run_tag_filter(conn, "feed", FEED, "  , #, -")
    assert "error" in result


def test_automation_after_refresh_runs_and_logs(env):
    with main.get_meta_connection() as conn:
        main.add_highlight_keyword(conn, "feed", FEED, "-deals", "yellow",
                                   rule_type="tag_filter", enabled=1)

    main._run_automation_after_refresh({FEED, OTHER_FEED})

    assert _unread_ids() == {"e-linux", "e-win", "e-untagged"}
    with main.get_meta_connection() as conn:
        log = conn.execute(
            "SELECT rule_type, scope, scope_id, entries_affected FROM rule_run_log"
        ).fetchall()
    assert len(log) == 1
    assert log[0]["rule_type"] == "tag_filter"
    assert log[0]["entries_affected"] == 2


def test_disabled_rule_does_not_run(env):
    with main.get_meta_connection() as conn:
        main.add_highlight_keyword(conn, "feed", FEED, "-deals", "yellow",
                                   rule_type="tag_filter", enabled=0)
    main._run_automation_after_refresh({FEED})
    assert len(_unread_ids()) == 5


# --- post-header chip toggles (toggle_feed_tag_filter) ---

def _rule_keyword(conn):
    row = conn.execute(
        "SELECT keyword, enabled FROM highlight_keywords WHERE type='tag_filter'"
        " AND scope='feed' AND scope_id=?", (FEED,)
    ).fetchone()
    return (row["keyword"], row["enabled"]) if row else None


def test_toggle_creates_rule_disabled_no_apply(env):
    # Chips are a tuning surface: the rule is created OFF and nothing is
    # marked read until it's armed in Automation.
    with main.get_meta_connection() as conn:
        result = main.toggle_feed_tag_filter(conn, FEED, "deals", "-")
    assert result["spec"] == "-deals"
    assert result["active"] == {"deals": "-"}
    assert result["enabled"] is False
    assert result["applied_count"] == 0
    assert len(_unread_ids()) == 5  # untouched
    with main.get_meta_connection() as conn:
        assert _rule_keyword(conn) == ("-deals", 0)


def test_toggle_on_enabled_rule_applies_immediately(env):
    with main.get_meta_connection() as conn:
        main.add_highlight_keyword(conn, "feed", FEED, "-sponsored", "yellow",
                                   rule_type="tag_filter", enabled=1)
        result = main.toggle_feed_tag_filter(conn, FEED, "deals", "-")
    assert result["spec"] == "-sponsored, -deals"
    assert result["enabled"] is True
    assert result["applied_count"] == 2
    assert _unread_ids() == {"e-linux", "e-win", "e-untagged"}
    with main.get_meta_connection() as conn:
        assert _rule_keyword(conn) == ("-sponsored, -deals", 1)


def test_toggle_same_sign_removes_and_empty_deletes_rule(env):
    with main.get_meta_connection() as conn:
        main.toggle_feed_tag_filter(conn, FEED, "deals", "-")
        result = main.toggle_feed_tag_filter(conn, FEED, "deals", "-")
    assert result["spec"] == ""
    assert result["active"] == {}
    with main.get_meta_connection() as conn:
        assert _rule_keyword(conn) is None  # rule deleted when spec empties


def test_toggle_opposite_sign_flips(env):
    with main.get_meta_connection() as conn:
        main.toggle_feed_tag_filter(conn, FEED, "linux", "-")
        result = main.toggle_feed_tag_filter(conn, FEED, "linux", "+")
    assert result["spec"] == "+linux"
    assert result["active"] == {"linux": "+"}


def test_toggle_appends_to_existing_spec(env):
    with main.get_meta_connection() as conn:
        main.toggle_feed_tag_filter(conn, FEED, "linux", "+")
        result = main.toggle_feed_tag_filter(conn, FEED, "deals", "-")
    assert result["spec"] == "+linux, -deals"


def test_toggle_preserves_enabled_state(env):
    # Chip edits never flip the switch: a disabled rule stays disabled.
    with main.get_meta_connection() as conn:
        main.add_highlight_keyword(conn, "feed", FEED, "-deals", "yellow",
                                   rule_type="tag_filter", enabled=0)
        main.toggle_feed_tag_filter(conn, FEED, "sponsored", "-")
        assert _rule_keyword(conn) == ("-deals, -sponsored", 0)
    assert len(_unread_ids()) == 5


def test_toggle_invalid_input(env):
    with main.get_meta_connection() as conn:
        assert "error" in main.toggle_feed_tag_filter(conn, FEED, "  ", "-")
        assert "error" in main.toggle_feed_tag_filter(conn, FEED, "x", "!")


def test_normalize_strips_disallowed_chars():
    # Feed/page tags with punctuation outside the tag alphabet survive
    # normalized rather than vanishing (was: '&' rejected the whole tag).
    assert main.normalize_tag_value("AI & Machine Learning") == "ai-machine-learning"
    assert main.normalize_tag_value("Paramount+") == "paramount+"
    assert main.normalize_tag_value("C#") == "c#"
    assert main.normalize_tag_value("&&&") is None
