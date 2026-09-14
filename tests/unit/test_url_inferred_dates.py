"""Publish dates inferred from the entry URL, including the year-month shape.

blog.guitar-pro.com is why the month tier exists: 67 epoch-dated entries whose
permalinks say ``/2021/04/`` but whose pages carry only a ``dateModified``
("Last update: oct. 21, 2024" on a post published in April 2021). Reading the
page would have been wrong by three and a half years, so the URL is both the best
signal and the only honest one.

datagenetics.com is why the month tier also matches a month-name-plus-year slug
(``/blog/march112020/``): ~35 posts ship no ``<pubDate>`` and no date anywhere on
the article page either, and the number in the slug is a per-month sequence, not
the day — see ``_URL_MONTHNAME_YEAR_RE`` in main.py. The sequence number is still
used as the returned day (clamped to <=28): real same-month pubDates climb
monotonically with it, so a higher number reliably sorts later even though it is
not the true day — better than every post in a month colliding on the 1st.
"""

from __future__ import annotations

from datetime import datetime, timezone

import main


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


# ── full dates ──
def test_a_full_date_path_is_read_exactly():
    assert main.url_inferred_pubdate("https://x.test/2019/01/22/a-post/") == _utc(2019, 1, 22)


def test_the_month_tier_declines_a_full_date_path():
    """They are different jobs and must not both claim the same URL — the day tier
    is exact, so it answers first and this one has nothing to add."""
    assert main.url_inferred_pubmonth("https://x.test/2019/01/22/a-post/") is None


# ── year-month ──
def test_a_year_month_permalink_resolves_to_the_first_of_the_month():
    assert main.url_inferred_pubmonth("https://blog.guitar-pro.com/2021/04/how-do-i-play-power-chords/") == _utc(2021, 4, 1)


def test_the_day_tier_does_not_match_a_year_month_permalink():
    """Which is exactly why 67 Guitar Pro entries sat at the Unix epoch: the only
    URL tier required /YYYY/MM/DD/ and WordPress had not written a day."""
    assert main.url_inferred_pubdate("https://blog.guitar-pro.com/2021/04/how-do-i-play-power-chords/") is None


def test_an_opaque_post_id_yields_nothing():
    """The site's current URLs (/blog/p/25431-how-do-i-play-power-chords) carry no
    date at all, which is why the old permalink is the thing worth mining."""
    assert main.url_inferred_pubmonth("https://www.guitar-pro.com/blog/p/25431-how-do-i-play-power-chords") is None


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


# ── month-name-plus-year (datagenetics.com) ──
def test_a_monthname_slug_resolves_using_its_sequence_number_as_the_day():
    """march11 is the 11th March-2020 post, not the 11th of March — but using 11
    as the day still sorts it correctly after march1..march10 within the month,
    which colliding on the 1st would not."""
    assert main.url_inferred_pubmonth("http://datagenetics.com/blog/march112020/index.html") == _utc(2020, 3, 11)


def test_the_day_tier_still_declines_a_monthname_slug():
    """The day tier's job is an exact claim; it must not fire here even though
    the month tier now borrows this same number as an ordering hint."""
    assert main.url_inferred_pubdate("http://datagenetics.com/blog/march112020/index.html") is None


def test_a_two_digit_sequence_number_still_resolves():
    assert main.url_inferred_pubmonth("http://datagenetics.com/blog/june122020/index.html") == _utc(2020, 6, 12)


def test_a_sequence_number_past_28_is_clamped_to_a_valid_day():
    assert main.url_inferred_pubmonth("https://x.test/blog/february292021/post/") == _utc(2021, 2, 28)


def test_higher_sequence_numbers_sort_later_within_the_month():
    earlier = main.url_inferred_pubmonth("http://datagenetics.com/blog/march32020/index.html")
    later = main.url_inferred_pubmonth("http://datagenetics.com/blog/march102020/index.html")
    assert earlier is not None and later is not None
    assert earlier < later


def test_monthname_matching_is_case_insensitive():
    assert main.url_inferred_pubmonth("http://datagenetics.com/blog/March112020/index.html") == _utc(2020, 3, 11)


def test_a_monthname_slug_without_a_sequence_digit_is_not_a_match():
    """The regex requires month name + digit(s) + year run together; a bare
    'march2020' has no room for the required sequence digit and must not be
    misread as the year alone."""
    assert main.url_inferred_pubmonth("https://x.test/blog/march2020/post/") is None


def test_a_monthname_slug_with_an_implausible_year_is_refused():
    """1990 matches the regex's (19|20)\\d{2} shape but fails the same
    2000-2099 plausibility gate every other tier applies."""
    assert main.url_inferred_pubmonth("https://x.test/blog/march111990/post/") is None
