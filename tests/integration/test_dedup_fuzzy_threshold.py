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
                        lambda *a, **kw: (seen.append(kw["fuzzy_threshold"]), real(*a, **kw))[1])
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
                        lambda *a, **kw: seen.append(kw["fuzzy_threshold"]) or {"count": 0})
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
    assert main._clamp_min_title_words(None) == main._DEDUP_MIN_TITLE_WORDS == 5
    assert main._clamp_min_title_words("x") == 5  # ty: ignore[invalid-argument-type]  # deliberately malformed input, exercises the except branch


def test_after_refresh_automation_reads_the_word_floor(env, monkeypatch):
    with main.get_meta_connection() as conn:
        main.add_highlight_keyword(conn, "global", "", "fuzzy", "yellow", rule_type="deduplicate",
                                   enabled=1, dedup_min_title_words=7)
    seen: list[int] = []
    monkeypatch.setattr(main, "_run_now_dedup",
                        lambda *a, **kw: seen.append(kw["min_title_words"]) or {"count": 0})
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
    # Pin the value, not just the agreement: two inputs normalizing to the same
    # WRONG string would satisfy equality alone.
    assert a == b == "apple ships a new laptop"


def test_four_word_rules_are_bumped_once_but_a_deliberate_four_survives(env):
    """The column shipped pre-filled with 4, so nobody had chosen it; the startup
    bump raises those to 5. A 4 set deliberately AFTER the bump must stick."""
    with main.get_meta_connection() as conn:
        main.add_highlight_keyword(conn, "global", "", "fuzzy", "yellow",
                                   rule_type="deduplicate", enabled=1, dedup_min_title_words=4)
        conn.execute("DELETE FROM app_settings WHERE key = 'dedup_min_title_words_bumped_to_5'")
    main._app_settings_cache.clear()   # the flag is read through a per-user cache
    main.ensure_meta_schema()
    with main.get_meta_connection() as conn:
        assert main.get_highlight_keywords(conn)[0]["dedup_min_title_words"] == 5
        assert main.get_setting(conn, "dedup_min_title_words_bumped_to_5") == "1"
        main.add_highlight_keyword(conn, "global", "", "fuzzy", "yellow",
                                   rule_type="deduplicate", enabled=1, dedup_min_title_words=4)
    main.ensure_meta_schema()          # a later startup must not re-bump it
    with main.get_meta_connection() as conn:
        assert main.get_highlight_keywords(conn)[0]["dedup_min_title_words"] == 4


# --- the preview counts read entries; the rule only acts on unread -----------


def test_preview_reports_how_many_matches_are_actionable(env):
    """The dry run deliberately scans read entries too — a folder whose dupes are
    already marked would otherwise preview as a bare zero, with nothing to tune a
    threshold against. So it has to say how many are UNREAD, which is all the rule
    will touch. Reported as 12 pairs / 0 actionable, this cost Josh a Run Now and
    a 'no matching unread entries' toast."""
    reader = main.get_reader()
    # Both copies: the two entries share a timestamp, so which one the run picks as
    # the keeper follows set-iteration order and flips between runs.
    reader.mark_entry_as_read((FEED_A, "e-a"))
    reader.mark_entry_as_read((FEED_B, "e-b"))
    with main.get_meta_connection() as conn:
        res = main._dry_run_dedup(conn, "global", "", "fuzzy", 168,
                                  custom_feed_urls={FEED_A, FEED_B}, fuzzy_threshold=0.60)
    assert res["total_would_mark_read"] == 1
    assert res["total_unread_would_mark_read"] == 0


def test_all_unread_matches_report_the_same_number(env):
    with main.get_meta_connection() as conn:
        res = main._dry_run_dedup(conn, "global", "", "fuzzy", 168,
                                  custom_feed_urls={FEED_A, FEED_B}, fuzzy_threshold=0.60)
    assert res["total_would_mark_read"] == res["total_unread_would_mark_read"] == 1


def test_a_pair_whose_older_copy_is_read_is_not_actionable(env):
    """Run Now loads unread entries only, so a group is reproduced there by its
    unread members alone — and one of THOSE becomes the keeper. With only one
    unread copy left the pair never forms, which is why counting the unread mark
    on its own promised a mark that never arrived."""
    reader = main.get_reader()
    with main.get_meta_connection() as conn:
        before = main._dry_run_dedup(conn, "global", "", "fuzzy", 168,
                                     custom_feed_urls={FEED_A, FEED_B}, fuzzy_threshold=0.60)
        reader.mark_entry_as_read((FEED_A, "e-a"))
        after = main._dry_run_dedup(conn, "global", "", "fuzzy", 168,
                                    custom_feed_urls={FEED_A, FEED_B}, fuzzy_threshold=0.60)
    assert before["total_unread_would_mark_read"] == 1
    assert after["total_would_mark_read"] == 1          # still previewed
    assert after["total_unread_would_mark_read"] == 0   # but nothing Run Now could do


def test_three_copies_with_a_read_oldest_still_leave_one_mark(env):
    """Two unread copies remain: Run Now keeps one and marks the other."""
    reader = main.get_reader()
    when = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)   # the fixture's epoch:
    for host, offset in (("c", 6), ("d", 8)):                # a later one falls outside
                                                             # the 168h window entirely
        feed = f"https://{host}.test/feed"
        reader.add_feed(feed, allow_invalid_url=True)
        reader.add_entry({"feed_url": feed, "id": f"e-{host}",
                          "title": "alpha beta gamma delta epsilon",
                          "link": f"{feed}/e-{host}", "summary": "x",
                          "published": when + dt.timedelta(hours=offset)})
    reader.mark_entry_as_read((FEED_A, "e-a"))          # the oldest copy
    feeds = {FEED_A, "https://c.test/feed", "https://d.test/feed"}
    with main.get_meta_connection() as conn:
        res = main._dry_run_dedup(conn, "global", "", "title", 168, custom_feed_urls=feeds)
    assert res["total_would_mark_read"] == 2            # preview counts all three
    assert res["total_unread_would_mark_read"] == 1     # Run Now keeps one unread, marks one
