"""Image harvesting used to scan the RAW fetched source page for <img> tags,
not just the extracted article -- so every image on the page (nav, sidebar,
related-posts widgets, footer, author headshots) got archived alongside
whatever the article actually contains. Reported live 2026-09-12: a Dropbox
blog post whose real content (readability_html) has zero <img> tags had 1053
assets archived (145MB) this way, because the raw page's chrome alone has
1620 <img> tags. Fixed by dropping source_html from the image-URL scan --
content_html/summary_html/readability_html (what's actually rendered) are the
only sources scanned for images now. source_html stays available for the
separate, policy-gated linked-FILE attachment scan, which doesn't share this
unconditional-image-grab cost."""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import services.starred_archive as starred_archive
from services.starred_archive import StarredArchiveService

FEED = "https://example.test/feed"


class _FakeReader:
    def __init__(self, entry):
        self._entry = entry

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_entry(self, key, default=None):
        return self._entry if key == (FEED, self._entry.id) else default


def _archive_conn_factory(tmp_path):
    path = tmp_path / "archive.sqlite"
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE archived_entry (
            feed_url TEXT NOT NULL, entry_id TEXT NOT NULL, status TEXT NOT NULL,
            starred_at REAL NOT NULL, archived_at REAL, error TEXT,
            source_html_zlib BLOB, readability_html_zlib BLOB, content_html_zlib BLOB,
            title TEXT, link TEXT, feed_title TEXT, author TEXT,
            published_at REAL, received_at REAL, content_size_bytes INTEGER,
            PRIMARY KEY (feed_url, entry_id)
        )
        """
    )
    conn.execute(
        "CREATE TABLE archived_asset (asset_hash TEXT PRIMARY KEY, data BLOB NOT NULL,"
        " content_type TEXT NOT NULL, width INTEGER, height INTEGER,"
        " byte_size INTEGER NOT NULL, created_at REAL NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE archived_asset_link (feed_url TEXT NOT NULL, entry_id TEXT NOT NULL,"
        " source_url TEXT NOT NULL, asset_hash TEXT NOT NULL,"
        " PRIMARY KEY (feed_url, entry_id, source_url))"
    )
    conn.commit()
    conn.close()

    def connect():
        c = sqlite3.connect(str(path))
        c.row_factory = sqlite3.Row
        return c

    return connect


def _service(tmp_path):
    archive_connect = _archive_conn_factory(tmp_path)
    return StarredArchiveService(
        get_archive_connection=archive_connect,
        get_meta_connection=lambda: sqlite3.connect(":memory:"),
        get_reader=lambda: None,  # overridden per-call below
        user_agent="test",
        sanitize_readability_html=lambda html: html,
    )


def _fetch_stub(source_html: str):
    def _fetch(url: str) -> tuple[str, str]:
        return source_html, url

    return _fetch


def _archive_asset_spy(calls: list[str]):
    def _spy(feed_url: str, entry_id: str, url: str, max_bytes: int | None = None) -> None:
        calls.append(url)

    return _spy


def _entry():
    return SimpleNamespace(
        id="e1",
        link="https://example.test/blog/post",
        enclosures=[],
        title="A real article",
        authors_str="",
        feed_resolved_title="",
        published=None,
        updated=None,
        added=None,
    )


def test_images_found_only_in_the_raw_page_chrome_are_not_archived(tmp_path, monkeypatch):
    # The raw fetched page: a real article paragraph plus a "related posts"
    # rail and a footer, each carrying images that are not part of the article.
    source_html = (
        "<html><body>"
        "<nav><img src='https://cdn.test/logo.png'></nav>"
        "<article><p>Real article text.</p></article>"
        "<aside class='related'>"
        "<img src='https://cdn.test/related1.jpg'>"
        "<img src='https://cdn.test/related2.jpg'>"
        "</aside>"
        "<footer><img src='https://cdn.test/footer-badge.png'></footer>"
        "</body></html>"
    )
    # What readability extracts from that page -- the real article, no chrome.
    readability_html = "<p>Real article text.</p>"

    svc = _service(tmp_path)
    entry = _entry()
    svc._get_reader = lambda: _FakeReader(entry)
    svc._fetch_text_with_url = _fetch_stub(source_html)
    monkeypatch.setattr(
        starred_archive, "Document",
        lambda html: SimpleNamespace(summary=lambda html_partial=True: readability_html),
    )
    spy_calls: list[str] = []
    svc._archive_asset = _archive_asset_spy(spy_calls)

    svc._archive_entry(FEED, "e1")

    assert spy_calls == []  # readability_html has no <img> tags at all
    assert "https://cdn.test/logo.png" not in spy_calls
    assert "https://cdn.test/related1.jpg" not in spy_calls
    assert "https://cdn.test/footer-badge.png" not in spy_calls


def test_an_image_that_survives_into_readability_html_is_still_archived(tmp_path, monkeypatch):
    # Same chrome-heavy page, but this time the article itself has one image
    # and readability keeps it -- that one must still be archived.
    source_html = (
        "<html><body>"
        "<nav><img src='https://cdn.test/logo.png'></nav>"
        "<article><p>Text.</p><img src='https://cdn.test/diagram.png'></article>"
        "<aside class='related'><img src='https://cdn.test/related1.jpg'></aside>"
        "</body></html>"
    )
    readability_html = "<p>Text.</p><img src='https://cdn.test/diagram.png'>"

    svc = _service(tmp_path)
    entry = _entry()
    svc._get_reader = lambda: _FakeReader(entry)
    svc._fetch_text_with_url = _fetch_stub(source_html)
    monkeypatch.setattr(
        starred_archive, "Document",
        lambda html: SimpleNamespace(summary=lambda html_partial=True: readability_html),
    )
    spy_calls: list[str] = []
    svc._archive_asset = _archive_asset_spy(spy_calls)

    svc._archive_entry(FEED, "e1")

    assert spy_calls == ["https://cdn.test/diagram.png"]


def test_linked_file_attachment_scan_still_sees_source_html(tmp_path, monkeypatch):
    # The image-scope fix must not touch the separate, policy-gated
    # linked-FILE scan (4b), which legitimately still needs the raw page --
    # a .pdf or .gp tab linked in the page chrome (not just the article) is
    # still something the user might want kept if their feed opts in.
    source_html = "<html><body><a href='https://cdn.test/manual.pdf'>Manual</a></body></html>"
    readability_html = "<p>Text.</p>"

    seen_html: list[str] = []

    def fake_find_attachments(feed_url, html, base):
        seen_html.append(html)
        return ["https://cdn.test/manual.pdf"] if "manual.pdf" in html else []

    archive_connect = _archive_conn_factory(tmp_path)
    svc = StarredArchiveService(
        get_archive_connection=archive_connect,
        get_meta_connection=lambda: sqlite3.connect(":memory:"),
        get_reader=lambda: None,
        user_agent="test",
        sanitize_readability_html=lambda html: html,
        find_attachments=fake_find_attachments,
    )
    entry = _entry()
    svc._get_reader = lambda: _FakeReader(entry)
    svc._fetch_text_with_url = _fetch_stub(source_html)
    monkeypatch.setattr(
        starred_archive, "Document",
        lambda html: SimpleNamespace(summary=lambda html_partial=True: readability_html),
    )
    spy_calls: list[str] = []
    svc._archive_asset = _archive_asset_spy(spy_calls)

    svc._archive_entry(FEED, "e1")

    assert "https://cdn.test/manual.pdf" in spy_calls
    assert source_html in seen_html
