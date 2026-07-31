"""The archive-old-stars decision layer.

The Inbox is starred-minus-archived and opened at 9,979 items, because years of
stars were made when a star meant "keep this" rather than "to-do". Renaming the
axis does not reclassify them, so this declares bankruptcy on the ones plainly
never getting read — by archiving, which keeps the tag, the capture and
pruning-exemption, rather than by unstarring, which throws the to-do axis away.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.archive_old_stars import age_buckets, build_archive_plan

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)


def _ago(days: int) -> datetime:
    return NOW - timedelta(days=days)


A, B, C, D = ("f", "a"), ("f", "b"), ("f", "c"), ("f", "d")


def test_only_stars_older_than_the_cutoff_are_archived():
    plan = build_archive_plan(
        {A: _ago(45), B: _ago(2)}, set(), days=30, now=NOW,
    )
    assert plan["to_archive"] == [A]
    assert plan["totals"]["to_archive"] == 1
    assert plan["totals"]["remaining"] == 1


def test_already_archived_entries_are_skipped():
    """They have no star left to discharge, and re-archiving would rewrite the
    date the user set when they actually filed it."""
    plan = build_archive_plan({A: _ago(45)}, {A}, days=30, now=NOW)
    assert plan["to_archive"] == []
    assert plan["totals"]["already_archived"] == 1


def test_a_missing_star_date_is_left_alone():
    """A missing timestamp is not evidence of age. Guessing here would archive an
    item the user starred minutes ago."""
    plan = build_archive_plan({A: None, B: _ago(45)}, set(), days=30, now=NOW)
    assert plan["to_archive"] == [B]


def test_naive_timestamps_are_treated_as_utc():
    """SQLite's CURRENT_TIMESTAMP writes naive UTC. Reading it as local time
    shifts the cutoff by the host's offset, which silently moves the boundary for
    everything starred near it.
    """
    naive_old = (NOW - timedelta(days=31)).replace(tzinfo=None)
    naive_new = (NOW - timedelta(days=1)).replace(tzinfo=None)
    plan = build_archive_plan({A: naive_old, B: naive_new}, set(), days=30, now=NOW)
    assert plan["to_archive"] == [A]


def test_boundary_is_strictly_older_than_the_cutoff():
    exactly = NOW - timedelta(days=30)
    plan = build_archive_plan({A: exactly}, set(), days=30, now=NOW)
    assert plan["to_archive"] == []      # 'older than 30 days' excludes exactly 30


def test_buckets_let_the_total_be_checked():
    """A single total is impossible to sanity-check before 9,000 items move."""
    buckets = age_buckets(
        {A: _ago(3), B: _ago(20), C: _ago(200), D: _ago(500)}, set(), now=NOW,
    )
    by_label = {b["label"]: b["count"] for b in buckets}
    assert by_label["under a week"] == 1
    assert by_label["1 week – 1 month"] == 1
    assert by_label["3–12 months"] == 1
    assert by_label["over a year"] == 1


def test_buckets_exclude_archived_items():
    buckets = age_buckets({A: _ago(200)}, {A}, now=NOW)
    assert sum(b["count"] for b in buckets) == 0


def test_days_is_clamped_to_at_least_one():
    """days=0 would mean 'archive everything including what I starred a second
    ago', which is never the intent of a cutoff control."""
    plan = build_archive_plan({A: _ago(0)}, set(), days=0, now=NOW)
    assert plan["to_archive"] == []
