"""List rows carry the chips the entry pane would show (publisher tags, minus dismissed), never the feed's pinned tags."""

from __future__ import annotations

import pytest
from _tenancy_helpers import configure_test_tenancy

import main
from services import tenancy

FEED = "https://example.test/feed"


@pytest.fixture
def tenant(tmp_path):
    saved = tenancy._layout
    main.close_thread_db_pools()
    configure_test_tenancy(tmp_path)
    main.ensure_meta_schema()
    yield
    main.close_thread_db_pools()
    tenancy._layout = saved


def test_chip_tags_normalized_and_dismissals_respected(tenant):
    main.feed_tag_service.record_entry_tags(FEED, [("e1", ["Popular Deals", "Games", "Noise"]), ("e2", ["Games"])])
    main.feed_tag_service.set_tag_suppressed(FEED, "noise", True)
    main.feed_tag_service.set_tag_globally_suppressed("games", True)
    out = main._chip_tags_for_rows([(FEED, "e1"), (FEED, "e2"), (FEED, "none")])
    assert out == {(FEED, "e1"): ["popular-deals"]}


def test_feed_wide_suppression_hides_all_chips(tenant):
    main.feed_tag_service.record_entry_tags(FEED, [("e1", ["Games"])])
    main.feed_tag_service.set_tag_suppressed(FEED, main.FEED_TAGS_SUPPRESS_ALL, True)
    assert main._chip_tags_for_rows([(FEED, "e1")]) == {}


def test_dismissed_tags_are_removed_before_the_cap(tenant):
    """More tags than the cap: a chip the pane shows (after dismissals) must not fall off the end of the row's chip list."""
    tags = [f"t{i}" for i in range(main.MAX_FEED_TAG_SUGGESTIONS)] + ["late"]
    main.feed_tag_service.record_entry_tags(FEED, [("e1", tags)])
    main.feed_tag_service.set_tag_suppressed(FEED, "t0", True)
    chips = main._chip_tags_for_rows([(FEED, "e1")])[(FEED, "e1")]
    assert "late" in chips and "t0" not in chips and len(chips) == main.MAX_FEED_TAG_SUGGESTIONS
