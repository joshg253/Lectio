"""A date the page prints with no semantic markup at all.

The labelled matcher wants an element whose class/id says "date"/"posted"/…,
which utility-CSS frameworks never provide. chickensoft.games renders its byline
as

    <p class="text-[var(--color-muted-foreground)] text-sm font-serif">April 26, 2026</p>

so every scraped entry fell back to the scrape timestamp and a fresh FakeFeed
looked like its whole backlog was published the second it was added.

The fallback tier keys on a different signal: the element's ENTIRE text is a
date. That has to stay strict, or a page full of dates (comment timestamps,
"related posts", a copyright footer) starts winning.
"""
from __future__ import annotations

import pytest

from services import publish_date as pd


def test_a_tailwind_byline_date_is_found():
    html = '<p class="text-[var(--color-muted-foreground)] text-sm font-serif">April 26, 2026</p>'
    found = pd.from_visible_text(html)
    assert found is not None
    assert found.date().isoformat() == "2026-04-26"


def test_a_labelled_date_still_wins_over_a_bare_one():
    """The fallback runs only after the labelled pass, so a publisher that marks
    its date up properly is still believed first."""
    html = ('<p class="whatever">April 26, 2026</p>'
            '<span class="post-date">March 1, 2020</span>')
    found = pd.from_visible_text(html)
    assert found is not None
    assert found.date().isoformat() == "2020-03-01"


@pytest.mark.parametrize("text", [
    "Updated April 26, 2026 by Chris",
    "Posted on April 26, 2026",
    "In April 2026 we shipped a thing",
    "Copyright 2026",
])
def test_prose_containing_a_date_is_not_a_byline(text):
    assert pd.from_visible_text(f"<p>{text}</p>") is None


@pytest.mark.parametrize("text,expected", [
    ("April 26, 2026", "2026-04-26"),
    ("3 February 2026", "2026-02-03"),
    ("2026-02-03", "2026-02-03"),
])
def test_each_supported_date_shape_works_bare(text, expected):
    found = pd.from_visible_text(f"<div>{text}</div>")
    assert found is not None
    assert found.date().isoformat() == expected


def test_out_of_range_dates_are_still_rejected():
    """Range-checking applies to the fallback too — 1900 is a template default."""
    assert pd.from_visible_text("<p>January 1, 1900</p>") is None


def test_no_date_anywhere_returns_none():
    assert pd.from_visible_text("<p>Hello</p><div>No dates here</div>") is None
