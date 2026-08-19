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


@pytest.mark.parametrize("pct,expected", [
    (None, 0.80), (80, 0.80), (100, 1.0), (95, 0.95),
    (10, 0.50), (0, 0.50), (140, 1.0), ("x", 0.80),
])
def test_threshold_clamped_to_a_sane_range(pct, expected):
    assert main._dedup_fuzzy_threshold(pct) == pytest.approx(expected)
