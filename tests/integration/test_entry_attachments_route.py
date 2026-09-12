"""GET /entries/attachments end-to-end: kept attachments come from the
archive, available candidates come from the same content the article pane
actually renders (never the raw fetched page -- see the 2026-09-12
image-scope fix), and orphaned (feed-gone) entries fall back to the archived
readability capture.
"""
from __future__ import annotations

import pytest

import main
from services import tenancy

FEED = "https://example.test/feed"
ENTRY = "https://example.test/post"


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
    main.ensure_starred_archive_schema()
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def test_kept_and_available_for_a_live_entry(configured):
    content_html = (
        '<p>Body text.</p>'
        '<a href="https://cdn.test/kept.pdf">already kept</a>'
        '<a href="https://cdn.test/not-kept.epub">not kept yet</a>'
    )
    with main.get_reader() as reader:
        reader.add_feed(FEED, exist_ok=True)
        reader.add_entry({
            "feed_url": FEED, "id": ENTRY, "link": ENTRY, "title": "Post",
            "content": [{"value": content_html, "type": "text/html"}],
        })
    with main.archive_conn() as conn:
        conn.execute(
            "INSERT INTO archived_entry (feed_url, entry_id, status, starred_at) VALUES (?, ?, 'complete', 1.0)",
            (FEED, ENTRY),
        )
        conn.execute(
            "INSERT INTO archived_asset (asset_hash, data, content_type, byte_size, created_at)"
            " VALUES ('h1', x'00', 'application/pdf', 555, 1.0)"
        )
        conn.execute(
            "INSERT INTO archived_asset_link (feed_url, entry_id, source_url, asset_hash)"
            " VALUES (?, ?, 'https://cdn.test/kept.pdf', 'h1')",
            (FEED, ENTRY),
        )

    resp = main.entry_attachments_route(feed_url=FEED, entry_id=ENTRY)
    import json as _json
    data = _json.loads(resp.body.decode())

    assert data["ok"] is True
    assert [k["source_url"] for k in data["kept"]] == ["https://cdn.test/kept.pdf"]
    assert data["available"] == ["https://cdn.test/not-kept.epub"]


def test_orphan_entry_falls_back_to_archived_readability(configured):
    """The feed is gone from reader entirely -- content must come from the
    archive's readability capture, not an error."""
    with main.archive_conn() as conn:
        conn.execute(
            "INSERT INTO archived_entry (feed_url, entry_id, status, starred_at, link, readability_html_zlib)"
            " VALUES (?, ?, 'complete', 1.0, ?, ?)",
            (FEED, ENTRY, ENTRY, __import__("zlib").compress(
                b'<a href="https://cdn.test/orphaned.pdf">file</a>'
            )),
        )

    resp = main.entry_attachments_route(feed_url=FEED, entry_id=ENTRY)
    import json as _json
    data = _json.loads(resp.body.decode())

    assert data["ok"] is True
    assert data["kept"] == []
    assert data["available"] == ["https://cdn.test/orphaned.pdf"]
