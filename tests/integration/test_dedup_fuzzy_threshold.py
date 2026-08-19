"""Dedup compare: the fuzzy title match is a tunable % knob, not a fixed 80%.

Two titles sharing 4 of 6 distinct words score 0.67 Jaccard — below the default
threshold, above a loosened one."""
from __future__ import annotations

import datetime as dt
import json

import pytest

import main
from services import tenancy

FEED_A = "https://a.test/feed"
FEED_B = "https://b.test/feed"


@pytest.fixture
def env(tmp_path):
    saved = tenancy._layout
    main.close_thread_db_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    reader = main.get_reader()
    when = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
    for feed, eid, title in [
        (FEED_A, "e-a", "alpha beta gamma delta epsilon"),
        (FEED_B, "e-b", "alpha beta gamma delta zeta"),
    ]:
        reader.add_feed(feed, allow_invalid_url=True)
        reader.add_entry({"feed_url": feed, "id": eid, "title": title,
                          "link": f"{feed}/{eid}", "summary": "x", "published": when})
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _fuzzy_marks(threshold: float) -> int:
    with main.get_meta_connection() as conn:
        res = main._dry_run_dedup(conn, "global", "", "fuzzy", 168,
                                  custom_feed_urls={FEED_A, FEED_B}, fuzzy_threshold=threshold)
    return res["total_would_mark_read"]


def test_default_threshold_rejects_partial_title_overlap(env):
    assert _fuzzy_marks(0.80) == 0


def test_loosened_threshold_catches_it(env):
    assert _fuzzy_marks(0.60) == 1


def test_route_passes_the_percent_knob_through(env):
    def run(pct: int) -> int:
        res = main.rules_dry_run_route(
            type="deduplicate", scope="global", scope_id="", keyword="fuzzy",
            is_regex=0, search_in="title", dedup_window_hours=168,
            exclude_scope_ids="", fuzzy_pct=pct, feed_urls=f"{FEED_A},{FEED_B}",
            yt_include_shorts=1, yt_min_minutes=0, yt_max_minutes=0,
        )
        return json.loads(bytes(res.body))["total_would_mark_read"]

    assert run(80) == 0
    assert run(60) == 1


def test_route_without_a_percent_uses_the_default(env):
    """An older client, or any caller that omits the field, must get the same
    answer as one that sends the default explicitly."""
    def run(**kw) -> int:
        res = main.rules_dry_run_route(
            type="deduplicate", scope="global", scope_id="", keyword="fuzzy",
            is_regex=0, search_in="title", dedup_window_hours=168,
            exclude_scope_ids="", feed_urls=f"{FEED_A},{FEED_B}",
            yt_include_shorts=1, yt_min_minutes=0, yt_max_minutes=0, **kw)
        return json.loads(bytes(res.body))["total_would_mark_read"]

    assert run() == run(fuzzy_pct=main._DEDUP_FUZZY_PCT_DEFAULT) == 0


@pytest.mark.parametrize("pct,expected", [
    (None, 0.80), (80, 0.80), (100, 1.0), (95, 0.95),
    (10, 0.50), (0, 0.50), (140, 1.0), ("x", 0.80),
])
def test_threshold_clamped_to_a_sane_range(pct, expected):
    assert main._dedup_fuzzy_threshold(pct) == pytest.approx(expected)


def test_rule_round_trips_the_saved_percent(env):
    with main.get_meta_connection() as conn:
        main.add_highlight_keyword(conn, "global", "", "fuzzy", "yellow",
                                   rule_type="deduplicate", enabled=1, dedup_fuzzy_pct=65)
        rule = [r for r in main.get_highlight_keywords(conn) if r["type"] == "deduplicate"][0]
    assert rule["dedup_fuzzy_pct"] == 65


def test_rule_percent_is_clamped_on_save(env):
    with main.get_meta_connection() as conn:
        main.add_highlight_keyword(conn, "global", "", "fuzzy", "yellow",
                                   rule_type="deduplicate", enabled=1, dedup_fuzzy_pct=5)
        rule = [r for r in main.get_highlight_keywords(conn) if r["type"] == "deduplicate"][0]
    assert rule["dedup_fuzzy_pct"] == 50


def test_default_percent_for_a_rule_saved_without_one(env):
    with main.get_meta_connection() as conn:
        main.add_highlight_keyword(conn, "global", "", "title", "yellow",
                                   rule_type="deduplicate", enabled=1)
        rule = [r for r in main.get_highlight_keywords(conn) if r["type"] == "deduplicate"][0]
    assert rule["dedup_fuzzy_pct"] == 80


def test_run_now_honors_the_saved_percent(env, monkeypatch):
    """The after-refresh automation reads the rule's column; Run Now sends it as a
    form field. Both land on _run_now_dedup's fuzzy_threshold."""
    seen: list[float] = []
    real = main._run_now_dedup
    monkeypatch.setattr(main, "_run_now_dedup",
                        lambda *a, **kw: (seen.append(kw.get("fuzzy_threshold")), real(*a, **kw))[1])
    main.rules_run_now_route(type="deduplicate", scope="global", scope_id="", keyword="fuzzy",
                             is_regex=0, search_in="title", dedup_window_hours=168,
                             exclude_scope_ids="", fuzzy_pct=65)
    assert seen == [pytest.approx(0.65)]


def test_after_refresh_automation_reads_the_column(env, monkeypatch):
    with main.get_meta_connection() as conn:
        main.add_highlight_keyword(conn, "global", "", "fuzzy", "yellow",
                                   rule_type="deduplicate", enabled=1, dedup_fuzzy_pct=65)
    seen: list[float] = []
    monkeypatch.setattr(main, "_run_now_dedup",
                        lambda *a, **kw: seen.append(kw.get("fuzzy_threshold")) or {"count": 0})
    main._run_automation_after_refresh({FEED_A})
    assert seen == [pytest.approx(0.65)]


# --- title-length floor -----------------------------------------------------


def test_short_titles_are_skipped_by_fuzzy(env):
    """Two feeds carrying the same 3-word title: below the floor, no match."""
    reader = main.get_reader()
    when = dt.datetime(2024, 5, 1, tzinfo=dt.timezone.utc)
    for feed, eid in [(FEED_A, "short-a"), (FEED_B, "short-b")]:
        reader.add_entry({"feed_url": feed, "id": eid, "title": "weekly open thread",
                          "link": f"{feed}/{eid}", "summary": "x", "published": when})
    with main.get_meta_connection() as conn:
        at4 = main._dry_run_dedup(conn, "global", "", "fuzzy", 168,
                                  custom_feed_urls={FEED_A, FEED_B}, fuzzy_threshold=0.80)
        at3 = main._dry_run_dedup(conn, "global", "", "fuzzy", 168,
                                  custom_feed_urls={FEED_A, FEED_B}, fuzzy_threshold=0.80,
                                  min_title_words=3)
    assert at4["total_would_mark_read"] == 0
    assert at3["total_would_mark_read"] == 1


def test_title_mode_now_has_a_floor(env):
    reader = main.get_reader()
    when = dt.datetime(2024, 5, 1, tzinfo=dt.timezone.utc)
    for feed, eid in [(FEED_A, "t-a"), (FEED_B, "t-b")]:
        reader.add_entry({"feed_url": feed, "id": eid, "title": "Open Thread",
                          "link": f"{feed}/{eid}", "summary": "x", "published": when})
    with main.get_meta_connection() as conn:
        res = main._dry_run_dedup(conn, "global", "", "title", 168,
                                  custom_feed_urls={FEED_A, FEED_B})
        low = main._dry_run_dedup(conn, "global", "", "title", 168,
                                  custom_feed_urls={FEED_A, FEED_B}, min_title_words=2)
    assert res["total_would_mark_read"] == 0
    assert low["total_would_mark_read"] == 1


def test_min_words_round_trips_and_clamps(env):
    with main.get_meta_connection() as conn:
        main.add_highlight_keyword(conn, "global", "", "fuzzy", "yellow", rule_type="deduplicate",
                                   enabled=1, dedup_min_title_words=6)
        assert [r for r in main.get_highlight_keywords(conn)][0]["dedup_min_title_words"] == 6
        main.add_highlight_keyword(conn, "global", "", "fuzzy", "yellow", rule_type="deduplicate",
                                   enabled=1, dedup_min_title_words=99)
        assert [r for r in main.get_highlight_keywords(conn)][0]["dedup_min_title_words"] == 10
    assert main._clamp_min_title_words(1) == 3
    assert main._clamp_min_title_words(None) == 4
    assert main._clamp_min_title_words("x") == 4


def test_after_refresh_automation_reads_the_word_floor(env, monkeypatch):
    with main.get_meta_connection() as conn:
        main.add_highlight_keyword(conn, "global", "", "fuzzy", "yellow", rule_type="deduplicate",
                                   enabled=1, dedup_min_title_words=7)
    seen: list[int] = []
    monkeypatch.setattr(main, "_run_now_dedup",
                        lambda *a, **kw: seen.append(kw.get("min_title_words")) or {"count": 0})
    main._run_automation_after_refresh({FEED_A})
    assert seen == [7]


# --- title normalization ----------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("best.cat()", "best.cat"),                       # word boundaries never move
    ("second-best.cat()", "second-best.cat"),
    ("C++ vs C# in 2025", "c++ vs c# in 2025"),       # + and # are the title
    ("AT&T buys #hashtag", "at&t buys #hashtag"),
    ("state-of-the-art e-mail", "state-of-the-art e-mail"),
    ("Title—Subtitle here", "title subtitle here"),   # em dash separates
    ("Don't panic — really", "don't panic really"),   # lone dash is dropped
    ("“Quoted” Words Are Fine!", "quoted words are fine"),   # quotes fold, then strip
    ("node.js 3.5 released", "node.js 3.5 released"),
    ("  Hello   World!  ", "hello world"),
])
def test_title_normalization(raw, expected):
    assert main.normalize_entry_title_for_dedupe(raw) == expected


def test_punctuation_no_longer_costs_a_match():
    a = main.normalize_entry_title_for_dedupe("Apple Ships a New Laptop!")
    b = main.normalize_entry_title_for_dedupe("Apple ships a new laptop.")
    assert a == b
