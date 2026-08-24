"""A YouTube description is plain text that still arrives HTML-escaped.

The entry pane's embed branch linkifies a tag-less body, and it escaped that
body again — so "Sources &amp; further reading" reached the browser as
"&amp;amp;" and rendered as a literal "&amp;"."""
from __future__ import annotations

import datetime as dt

import pytest

import main
from services import tenancy

FEED = "https://www.youtube.com/feeds/videos.xml?channel_id=UCtest"
VIDEO = "w5RZfxQHxyY"
SUMMARY = ("Antidepressants change how brain cells communicate.\n"
           "Sources &amp; further reading: https://example.test/sources?a=1&amp;b=2\n"
           "DISCUSSIONS &amp; SOCIAL MEDIA")


@pytest.fixture
def env(tmp_path, monkeypatch):
    # get_entry_detail falls back to the SOURCE PAGE for tag suggestions and lead
    # images. A test must never reach for youtube.com, so the outbound URL guard
    # refuses everything here — the branch under test does not depend on it.
    monkeypatch.setattr(main.url_guard, "is_safe_outbound_url", lambda *a, **kw: False)
    saved = tenancy._layout
    main.close_thread_db_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    reader = main.get_reader()
    reader.add_feed(FEED, allow_invalid_url=True)
    reader.add_entry({
        "feed_url": FEED, "id": f"yt:video:{VIDEO}",
        "link": f"https://www.youtube.com/watch?v={VIDEO}",
        "title": "How Antidepressants Work", "summary": SUMMARY,
        "published": dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc),
    })
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _content_html() -> str:
    detail = main.get_entry_detail(FEED, f"yt:video:{VIDEO}")
    assert detail is not None
    return detail.get("content_html") or ""


def test_the_embed_is_injected(env):
    assert "/embed/" in _content_html()


def test_ampersands_are_escaped_exactly_once(env):
    html_out = _content_html()
    assert "&amp;amp;" not in html_out
    assert "Sources &amp; further reading" in html_out


def test_the_reader_sees_a_real_ampersand(env):
    from bs4 import BeautifulSoup

    text = BeautifulSoup(_content_html(), "html.parser").get_text(" ", strip=True)
    assert "Sources & further reading" in text
    assert "&amp;" not in text


def test_bare_urls_still_become_links(env):
    assert 'href="https://example.test/sources?a=1&amp;b=2"' in _content_html()
