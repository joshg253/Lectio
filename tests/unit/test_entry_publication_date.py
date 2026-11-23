"""Publication dates: sentinels are not dates, and inference must be reachable.

Two defects met here and hid each other (found 2026-08-04):

  * a missing date stored as the Unix epoch (or year 0001) is *truthy*, so it won
    every `or` chain instead of falling through like the NULL it stands for;
  * the URL/title inference was dead code — it sat behind `entry_effective_date`,
    which already fell back to the received date and so was never falsy.

The visible result was an entry with `2025-11-22` in its own URL displaying no
usable date at all.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import main


class _Entry:
    """Just the attributes the date helpers read."""

    def __init__(self, *, published=None, updated=None, link=None, id=None, title=None, added=datetime(2026, 8, 4, tzinfo=timezone.utc)):
        self.published = published
        self.updated = updated
        self.link = link
        self.id = id
        self.title = title
        self.added = added


# --- sentinel rejection ----------------------------------------------------


@pytest.mark.parametrize("year", [1970, 1])
def test_sentinel_dates_are_not_publication_dates(year):
    """The two clusters that exist in the real library: an importer's epoch 0
    and a parser's year-0001 zero value."""
    assert main.real_published_date(datetime(year, 1, 1)) is None


@pytest.mark.parametrize(
    "value",
    [
        datetime(2025, 11, 22),
        datetime(1995, 6, 1),
        datetime(2025, 11, 22, tzinfo=timezone.utc),
    ],
)
def test_real_dates_survive(value):
    assert main.real_published_date(value) == value


def test_none_stays_none():
    assert main.real_published_date(None) is None


# --- the /YYYY-MM-DD/ permalink shape --------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        # Jekyll/Hugo and hand-rolled blogs — the shape that was unsupported.
        ("https://www.brendangregg.com/blog/2025-11-22/intel-is-listening.html", datetime(2025, 11, 22, tzinfo=timezone.utc)),
        # WordPress, which already worked and must keep working.
        ("https://example.test/2019/07/06/post.html", datetime(2019, 7, 6, tzinfo=timezone.utc)),
        ("https://example.test/2019/7/6/", datetime(2019, 7, 6, tzinfo=timezone.utc)),
    ],
)
def test_dated_permalinks_are_read(url, expected):
    assert main.url_inferred_pubdate(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/1234-56-78/post",  # month 56 is not a month
        "https://example.test/1999-01-01/post",  # before the supported range
        "https://example.test/no-date-here/post",
    ],
)
def test_non_dates_are_rejected(url):
    assert main.url_inferred_pubdate(url) is None


# --- the chain, end to end -------------------------------------------------


def test_epoch_published_no_longer_blocks_url_inference():
    """The exact reported case: epoch-0 stored, real date in the URL."""
    e = _Entry(published=datetime(1970, 1, 1), link="https://www.brendangregg.com/blog/2025-11-22/intel-is-listening.html")

    assert main.entry_publication_date(e) == datetime(2025, 11, 22, tzinfo=timezone.utc)
    assert main.entry_effective_date(e) == datetime(2025, 11, 22, tzinfo=timezone.utc)


def test_date_can_come_from_the_id_when_the_link_has_none():
    """link and id can differ and the date may live in either."""
    e = _Entry(published=datetime(1970, 1, 1), link="https://example.test/permalink", id="https://example.test/2021/03/09/real-slug")

    assert main.entry_publication_date(e) == datetime(2021, 3, 9, tzinfo=timezone.utc)


def test_month_precision_is_used_only_after_day_precision():
    e = _Entry(link="https://example.test/2021/04/thing/")
    assert main.entry_publication_date(e) == datetime(2021, 4, 1, tzinfo=timezone.utc)


def test_a_real_feed_date_still_wins_over_the_url():
    """Inference is a fallback, never an override — a publisher's own date is
    better than anything guessed from a permalink."""
    e = _Entry(published=datetime(2020, 5, 5), link="https://example.test/2019/07/06/post.html")

    assert main.entry_publication_date(e) == datetime(2020, 5, 5)


def test_undatable_entry_reports_no_publication_date():
    """entry_publication_date must be able to say "nothing" — that is what lets
    the UI show "no date" instead of quietly showing the received date."""
    assert main.entry_publication_date(_Entry()) is None


def test_effective_date_still_always_returns_something():
    """The sort and the bulk age actions rely on this never being None."""
    received = datetime(2026, 8, 4, tzinfo=timezone.utc)
    assert main.entry_effective_date(_Entry(added=received)) == received


def test_effective_date_falls_back_past_a_sentinel():
    """Before the fix this returned 1970 and sorted the entry to the far end
    forever — 312 entries did exactly that."""
    received = datetime(2026, 8, 4, tzinfo=timezone.utc)
    e = _Entry(published=datetime(1970, 1, 1), added=received)

    assert main.entry_effective_date(e) == received


# --- feed dates ahead of the received time ---------------------------------


def test_feed_date_far_ahead_of_received_is_clamped_to_received():
    """Some publishers stamp "today" at a fixed future hour (e.g. noon UTC), so an
    entry arrives dated hours ahead of when we received it."""
    received = datetime(2026, 11, 22, 22, 37, tzinfo=timezone.utc)
    e = _Entry(published=datetime(2026, 11, 23, 17, 0), added=received)
    assert main.entry_publication_date(e) == received
    assert main.entry_effective_date(e) == received


def test_feed_date_within_slack_of_received_is_kept():
    received = datetime(2026, 11, 22, 22, 37, tzinfo=timezone.utc)
    published = datetime(2026, 11, 22, 23, 10)
    assert main.entry_publication_date(_Entry(published=published, added=received)) == published


def test_older_feed_date_is_kept():
    published = datetime(2026, 11, 1, 12, 0)
    received = datetime(2026, 11, 22, tzinfo=timezone.utc)
    assert main.entry_publication_date(_Entry(published=published, added=received)) == published


def test_later_updated_is_not_clamped():
    """`updated` legitimately advances after first fetch (publisher edits the item); only `published` is clamped."""
    received = datetime(2026, 11, 1, tzinfo=timezone.utc)
    updated = datetime(2026, 11, 20, 9, 0)
    assert main.entry_publication_date(_Entry(updated=updated, added=received)) == updated


def test_clamp_boundary_is_exactly_one_hour():
    received = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
    assert main.entry_publication_date(_Entry(published=datetime(2026, 11, 22, 13, 0), added=received)) == datetime(2026, 11, 22, 13, 0)
    assert main.entry_publication_date(_Entry(published=datetime(2026, 11, 22, 13, 0, 1), added=received)) == received


def test_aware_published_and_missing_added():
    aware = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
    received = datetime(2026, 11, 22, 12, 0)  # naive
    assert main.entry_publication_date(_Entry(published=aware, added=received)) == received
    assert main.entry_publication_date(_Entry(published=aware, added=None)) == aware


def test_sql_sort_key_mirrors_the_clamp():
    """The SQL fast paths order by `_ENTRY_SORT_SQL`; it must agree with the Python clamp or entries drop at window edges."""
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE entries (id TEXT, published TEXT, updated TEXT, first_updated TEXT)")
    rows = [
        ("future", "2026-11-23 17:00:00", None, "2026-11-22 22:37:57.4"),  # clamped -> first_updated
        ("within_slack", "2026-11-22 23:10:00", None, "2026-11-22 22:37:57.4"),  # kept
        ("updated_later", None, "2026-11-20 09:00:00", "2026-11-01 00:00:00"),  # updated never clamped
        ("undated", None, None, "2026-11-05 00:00:00"),
    ]
    db.executemany("INSERT INTO entries VALUES (?,?,?,?)", rows)
    got = dict(db.execute(f"SELECT id, {main._ENTRY_SORT_SQL} FROM entries"))
    assert got["future"] == "2026-11-22 22:37:57.4"
    assert got["within_slack"] == "2026-11-22 23:10:00"
    assert got["updated_later"] == "2026-11-20 09:00:00"
    assert got["undated"] == "2026-11-05 00:00:00"
