"""Tags pinned to a feed, offered as chips on every one of its posts.

Feed-provided chips only exist when the publisher ships them, and a feed with a
stable subject ships none — a guitar blog does not tag its posts "guitar" — so
filing them meant typing the same word on every post.
"""
from __future__ import annotations

import pytest

import main
from services import tenancy

FEED = "https://guitar.example.test/feed"


@pytest.fixture
def configured(tmp_path):
    saved = tenancy._layout
    main.close_thread_db_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    with main.get_reader() as reader:
        reader.add_feed(FEED, exist_ok=True)
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def test_no_pinned_tags_by_default(configured):
    assert main.get_feed_pinned_tags(FEED) == []


def test_round_trip(configured):
    assert main.set_feed_pinned_tags(FEED, "guitar bass") == ["guitar", "bass"]
    assert main.get_feed_pinned_tags(FEED) == ["guitar", "bass"]


def test_order_is_preserved(configured):
    """These are offered as chips in this order, so it is the user's choice."""
    main.set_feed_pinned_tags(FEED, "bass guitar amps")
    assert main.get_feed_pinned_tags(FEED) == ["bass", "guitar", "amps"]


@pytest.mark.parametrize("raw,expected", [
    ("#guitar #bass", ["guitar", "bass"]),      # leading hashes
    ("guitar, bass", ["guitar", "bass"]),        # commas
    ("Guitar BASS", ["guitar", "bass"]),         # case
    ("guitar   bass", ["guitar", "bass"]),       # runs of whitespace
    ("guitar guitar bass", ["guitar", "bass"]),  # repeats
])
def test_input_is_normalized(configured, raw, expected):
    assert main.set_feed_pinned_tags(FEED, raw) == expected


def test_clearing(configured):
    main.set_feed_pinned_tags(FEED, "guitar")
    assert main.set_feed_pinned_tags(FEED, "") == []
    assert main.get_feed_pinned_tags(FEED) == []


def test_pinned_tags_are_per_feed(configured):
    other = "https://bass.example.test/feed"
    with main.get_reader() as reader:
        reader.add_feed(other, exist_ok=True)
    main.set_feed_pinned_tags(FEED, "guitar")
    main.set_feed_pinned_tags(other, "bass")

    assert main.get_feed_pinned_tags(FEED) == ["guitar"]
    assert main.get_feed_pinned_tags(other) == ["bass"]


def test_pinned_tags_survive_other_display_prefs(configured):
    """feed_display_prefs is shared with the image/thumb settings, so a write to
    one must not clear the other."""
    main.set_feed_pinned_tags(FEED, "guitar")
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO feed_display_prefs (feed_url, hide_shorts) VALUES (?, 1)"
            " ON CONFLICT(feed_url) DO UPDATE SET hide_shorts = 1", (FEED,))

    assert main.get_feed_pinned_tags(FEED) == ["guitar"]


# --- how they reach the chips ----------------------------------------------


def test_pinned_tags_come_first_and_only_once(configured, monkeypatch):
    """Both halves of the request: pinned first, and never a duplicate chip when
    the publisher happens to ship the same tag."""
    monkeypatch.setattr(main, "get_feed_tag_suggestions",
                        lambda f, e: ["lessons", "guitar", "review"])
    main.set_feed_pinned_tags(FEED, "guitar bass")

    pinned = main.get_feed_pinned_tags(FEED)
    tags: list[str] = []
    for raw in [*pinned, *main.get_feed_tag_suggestions(FEED, "e1")]:
        n = main.normalize_tag_value(raw)
        if n and n not in tags:
            tags.append(n)

    assert tags[:2] == ["guitar", "bass"], "pinned tags not offered first"
    assert tags.count("guitar") == 1, "a tag the publisher also ships was shown twice"
    assert tags == ["guitar", "bass", "lessons", "review"]
