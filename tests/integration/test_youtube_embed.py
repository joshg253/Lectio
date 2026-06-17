"""The inline YouTube player stopped playing because the embed set
``enablejsapi=1`` without an ``origin=`` parameter; YouTube now refuses playback
in that configuration. The app never drives the IFrame JS API, so the embed must
use YouTube's canonical markup (nocookie host, referrerpolicy, no enablejsapi).
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

import main
from services import tenancy

YT_FEED = "https://www.youtube.com/feeds/videos.xml?channel_id=UCABC123"
VIDEO_ID = "dQw4w9WgXcQ"
ENTRY_ID = "yt:video:dQw4w9WgXcQ"


def test_embed_markup_is_playable():
    html = main._youtube_embed_html(VIDEO_ID)
    # The bug: enablejsapi=1 without origin= blocks playback.
    assert "enablejsapi" not in html
    # Canonical YouTube embed: privacy host + referrerpolicy (authorizes the
    # embedding origin without the JS API).
    assert f"https://www.youtube-nocookie.com/embed/{VIDEO_ID}" in html
    assert 'referrerpolicy="strict-origin-when-cross-origin"' in html
    assert "allowfullscreen" in html


def _reset_reader_pool():
    main._reader_thread_local.pool = None


@pytest.fixture
def youtube_entry(tmp_path):
    saved = tenancy._layout
    _reset_reader_pool()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    main.ensure_yt_duration_schema()
    reader = main.get_reader()
    reader.add_feed(YT_FEED, allow_invalid_url=True)
    reader.add_entry(
        {
            "feed_url": YT_FEED,
            "id": ENTRY_ID,
            "title": "Test Video",
            "link": f"https://www.youtube.com/watch?v={VIDEO_ID}",
            "summary": "A description.",
            "published": dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
        }
    )
    try:
        yield
    finally:
        _reset_reader_pool()
        tenancy._layout = saved


def test_youtube_feed_entry_gets_playable_embed(youtube_entry):
    detail = main.get_entry_detail(YT_FEED, ENTRY_ID)
    assert detail is not None
    content_html = detail["content_html"]
    assert f"youtube-nocookie.com/embed/{VIDEO_ID}" in content_html
    assert "enablejsapi" not in content_html
