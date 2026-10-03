"""Retry intervals for lead-image re-scrapes grow with the post's age."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from services.lead_images import LeadImageService

NOW = datetime(2026, 10, 2, tzinfo=timezone.utc)
BASE = 4 * 3600


def _scaled(age_days, **kw):
    entry = SimpleNamespace(published=NOW - timedelta(days=age_days), updated=None, **kw)
    return LeadImageService._age_scaled_retry_seconds(entry, BASE, NOW.timestamp())


def test_fresh_posts_keep_the_base_interval():
    assert _scaled(1) == BASE


def test_older_posts_back_off_in_steps():
    assert _scaled(10) == BASE * 2
    assert _scaled(400) == BASE * 6


def test_naive_dates_are_read_as_utc():
    entry = SimpleNamespace(published=datetime(2020, 1, 1), updated=None)
    assert LeadImageService._age_scaled_retry_seconds(entry, BASE, NOW.timestamp()) == BASE * 6


def test_undated_posts_keep_the_base_interval():
    assert LeadImageService._age_scaled_retry_seconds(SimpleNamespace(published=None, updated=None), BASE, NOW.timestamp()) == BASE
