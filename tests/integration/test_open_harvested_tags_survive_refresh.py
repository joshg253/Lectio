"""Tags harvested from an article page on open must survive the feed re-delivering the entry untagged.

The feed refresh now sends ``(entry_id, [])`` for an entry with no tags so a feed that drops its tags clears the stored ``source='feed'``
rows. The on-open and background harvest writers therefore must not store as 'feed', or they would be wiped on every refresh.
"""

from __future__ import annotations

import pytest
from _tenancy_helpers import configure_test_tenancy

import main
from services import tenancy

FEED = "https://harvest.example.test/feed"
ENTRY = "https://harvest.example.test/post"
PAGE = '<html><head><meta property="article:tag" content="Rowing"></head><body></body></html>'


@pytest.fixture
def configured(tmp_path):
    saved = tenancy._layout
    main.close_thread_db_pools()
    configure_test_tenancy(tmp_path)
    main.ensure_meta_schema()
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def test_background_harvest_survives_untagged_redelivery(configured):
    main._persist_page_tags(FEED, ENTRY, PAGE, ENTRY)
    assert main.feed_tag_service.get_tags_for_entry(FEED, ENTRY) == ["Rowing"]
    main.feed_tag_service.record_entry_tags(FEED, [(ENTRY, [])])  # what the next refresh sends
    assert main.feed_tag_service.get_tags_for_entry(FEED, ENTRY) == ["Rowing"]
