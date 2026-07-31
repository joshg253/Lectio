"""Publish dates inferred from the entry URL, including the year-month shape.

blog.guitar-pro.com is why the month tier exists: 67 epoch-dated entries whose
permalinks say ``/2021/04/`` but whose pages carry only a ``dateModified``
("Last update: oct. 21, 2024" on a post published in April 2021). Reading the
page would have been wrong by three and a half years, so the URL is both the best
signal and the only honest one.
"""
from __future__ import annotations

from datetime import datetime, timezone

import main


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


# ── full dates ──
def test_a_full_date_path_is_read_exactly():
    assert main.url_inferred_pubdate(
        "https://x.test/2019/01/22/a-post/") == _utc(2019, 1, 22)


def test_the_month_tier_declines_a_full_date_path():
    """They are different jobs and must not both claim the same URL — the day tier
    is exact, so it answers first and this one has nothing to add."""
    assert main.url_inferred_pubmonth("https://x.test/2019/01/22/a-post/") is None


# ── year-month ──
def test_a_year_month_permalink_resolves_to_the_first_of_the_month():
    assert main.url_inferred_pubmonth(
        "https://blog.guitar-pro.com/2021/04/how-do-i-play-power-chords/"
    ) == _utc(2021, 4, 1)


def test_the_day_tier_does_not_match_a_year_month_permalink():
    """Which is exactly why 67 Guitar Pro entries sat at the Unix epoch: the only
    URL tier required /YYYY/MM/DD/ and WordPress had not written a day."""
    assert main.url_inferred_pubdate(
        "https://blog.guitar-pro.com/2021/04/how-do-i-play-power-chords/") is None


def test_an_opaque_post_id_yields_nothing():
    """The site's current URLs (/blog/p/25431-how-do-i-play-power-chords) carry no
    date at all, which is why the old permalink is the thing worth mining."""
    assert main.url_inferred_pubmonth(
        "https://www.guitar-pro.com/blog/p/25431-how-do-i-play-power-chords") is None


# ── refusals ──
def test_an_impossible_month_is_refused():
    assert main.url_inferred_pubmonth("https://x.test/2021/13/post/") is None


def test_a_year_outside_the_plausible_range_is_refused():
    """1999 and 2199 in a path are far more likely to be an id than a date."""
    assert main.url_inferred_pubmonth("https://x.test/1999/04/post/") is None
    assert main.url_inferred_pubmonth("https://x.test/2199/04/post/") is None


def test_nothing_in_nothing_out():
    assert main.url_inferred_pubmonth(None) is None
    assert main.url_inferred_pubmonth("") is None
    assert main.url_inferred_pubmonth("https://x.test/no/date/here/") is None
