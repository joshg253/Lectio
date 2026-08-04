"""Re-fetch from the Internet Archive by explicit request.

The automatic archive fallback only fires when the live fetch is *refused* — a
parked page or a 404. The case that sent users to archive.org by hand is the one
that passes every guard: a publisher serving a page that is no longer the
article (rewritten, truncated, paywalled). Nothing could ask for the archive.
"""
from __future__ import annotations

import pytest

import main
from services import tenancy

FEED = "https://example.test/feed"
ENTRY = "https://example.test/post"
SNAPSHOT = "http://web.archive.org/web/20200101000000/https://example.test/post"


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
        reader.add_entry({"feed_url": FEED, "id": ENTRY, "title": "post", "link": ENTRY})
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def test_archive_mode_fetches_the_snapshot_not_the_live_page(configured, monkeypatch):
    fetched: list[str] = []
    monkeypatch.setattr(main, "wayback_snapshot_url", lambda url: SNAPSHOT)

    def _fake_extract(url, capture=None):
        fetched.append(url)
        if capture is not None:
            capture["raw_html"] = "<html><body>archived</body></html>"
        return ("post", "<p>archived body</p>")

    monkeypatch.setattr(main, "fetch_readability_article", _fake_extract)
    monkeypatch.setattr(
        main.saved_articles_service, "refresh_captured_article",
        lambda reader, conn, f, e, extract, enqueue_archive: (
            extract(ENTRY), {"ok": True, "source_url": ENTRY})[1],
    )

    result = main._refresh_captured_article_for_current_user(
        FEED, ENTRY, mode=main.CAPTURE_MODE_ARCHIVE)

    assert result["ok"] is True
    assert fetched == [SNAPSHOT], "went to the live page instead of the snapshot"
    assert result["from_archive"] == SNAPSHOT


def test_archive_mode_says_so_when_there_is_no_snapshot(configured, monkeypatch):
    """Better than silently re-fetching the live page the user was trying to
    get around."""
    monkeypatch.setattr(main, "wayback_snapshot_url", lambda url: None)

    result = main._refresh_captured_article_for_current_user(
        FEED, ENTRY, mode=main.CAPTURE_MODE_ARCHIVE)

    assert result["ok"] is False
    assert "Internet Archive" in result["error"]


def test_normal_mode_still_fetches_the_live_page(configured, monkeypatch):
    fetched: list[str] = []
    monkeypatch.setattr(main, "wayback_snapshot_url",
                        lambda url: pytest.fail("asked the archive unprompted"))

    def _fake_extract(url, capture=None):
        fetched.append(url)
        return ("post", "<p>live body</p>")

    monkeypatch.setattr(main, "fetch_readability_article", _fake_extract)
    monkeypatch.setattr(
        main.saved_articles_service, "refresh_captured_article",
        lambda reader, conn, f, e, extract, enqueue_archive: (
            extract(ENTRY), {"ok": True, "source_url": ENTRY})[1],
    )

    main._refresh_captured_article_for_current_user(FEED, ENTRY)

    assert fetched == [ENTRY]


def test_entry_source_url_prefers_the_link(configured):
    assert main._entry_source_url(FEED, ENTRY) == ENTRY


def test_entry_source_url_is_none_for_a_missing_entry(configured):
    assert main._entry_source_url(FEED, "no-such-entry") is None
