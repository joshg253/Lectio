"""Epoch/placeholder feed dates are dropped at ingest, not stored as real ones."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

import pytest

from services.reader_sanitize import _drop_placeholder_date, _sanitize_entry


@dataclasses.dataclass(frozen=True)
class _Entry:
    published: datetime | None = None
    updated: datetime | None = None
    summary: str | None = None
    content: tuple = ()


EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
REAL = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "value",
    [
        EPOCH,
        datetime(1970, 1, 1),                      # naive, as feedparser may give
        datetime(1970, 6, 5, tzinfo=timezone.utc),  # still epoch-year junk
        datetime(1900, 1, 1, tzinfo=timezone.utc),  # template placeholder
    ],
)
def test_placeholder_dates_become_none(value):
    assert _drop_placeholder_date(value) is None


@pytest.mark.parametrize(
    "value",
    [
        REAL,
        datetime(1991, 1, 1, tzinfo=timezone.utc),  # old, but a real publication
        None,
        "not a datetime",
    ],
)
def test_real_values_pass_through_unchanged(value):
    assert _drop_placeholder_date(value) is value


def test_entry_published_is_nulled():
    out = _sanitize_entry(_Entry(published=EPOCH, updated=REAL))
    assert out.published is None
    assert out.updated == REAL


def test_entry_updated_is_nulled_too():
    out = _sanitize_entry(_Entry(published=REAL, updated=EPOCH))
    assert out.published == REAL
    assert out.updated is None


def test_a_good_entry_is_returned_unchanged():
    entry = _Entry(published=REAL, updated=REAL)
    assert _sanitize_entry(entry) is entry


def test_sanitization_still_happens_alongside():
    out = _sanitize_entry(_Entry(published=EPOCH, summary="<script>x</script><p>hi</p>"))
    assert out.published is None
    assert "<script>" not in (out.summary or "")
