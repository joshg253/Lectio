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


def test_enclosure_only_file_is_available_even_with_no_body_link(configured):
    """Full Circle Magazine's issue PDF/EPUB (and a devblog's demo video) are
    declared ONLY as an <enclosure> -- never linked anywhere in the article
    body. Reported live 2026-09-13: once un-kept (untag+unstar deletes the
    archive), such a file had no way back into the panel at all, because the
    "available" scan only ever looked at body links."""
    with main.get_reader() as reader:
        reader.add_feed(FEED, exist_ok=True)
        reader.add_entry({
            "feed_url": FEED, "id": ENTRY, "link": ENTRY, "title": "Issue 231",
            "content": [{"value": "<p>No download link here.</p>", "type": "text/html"}],
            "enclosures": [{"href": "https://cdn.test/issue-231.pdf", "type": "application/pdf", "length": 999}],
        })
    # Nothing archived at all -- the fully un-kept state after untag+unstar.
    resp = main.entry_attachments_route(feed_url=FEED, entry_id=ENTRY)
    import json as _json
    data = _json.loads(resp.body.decode())

    assert data["ok"] is True
    assert data["kept"] == []
    assert data["available"] == ["https://cdn.test/issue-231.pdf"]


def test_enclosure_audio_and_image_are_excluded_from_available(configured):
    with main.get_reader() as reader:
        reader.add_feed(FEED, exist_ok=True)
        reader.add_entry({
            "feed_url": FEED, "id": ENTRY, "link": ENTRY, "title": "Episode",
            "enclosures": [
                {"href": "https://cdn.test/ep.mp3", "type": "audio/mpeg", "length": 111},
                {"href": "https://cdn.test/cover.jpg", "type": "image/jpeg", "length": 222},
                {"href": "https://cdn.test/notes.pdf", "type": "application/pdf", "length": 333},
            ],
        })
    resp = main.entry_attachments_route(feed_url=FEED, entry_id=ENTRY)
    import json as _json
    data = _json.loads(resp.body.decode())

    assert data["available"] == ["https://cdn.test/notes.pdf"]


def test_enclosure_already_kept_is_not_duplicated_into_available(configured):
    with main.get_reader() as reader:
        reader.add_feed(FEED, exist_ok=True)
        reader.add_entry({
            "feed_url": FEED, "id": ENTRY, "link": ENTRY, "title": "Issue 231",
            "enclosures": [{"href": "https://cdn.test/issue-231.pdf", "type": "application/pdf", "length": 999}],
        })
    with main.archive_conn() as conn:
        conn.execute(
            "INSERT INTO archived_entry (feed_url, entry_id, status, starred_at) VALUES (?, ?, 'complete', 1.0)",
            (FEED, ENTRY),
        )
        conn.execute(
            "INSERT INTO archived_asset (asset_hash, data, content_type, byte_size, created_at)"
            " VALUES ('h1', x'00', 'application/pdf', 999, 1.0)"
        )
        conn.execute(
            "INSERT INTO archived_asset_link (feed_url, entry_id, source_url, asset_hash)"
            " VALUES (?, ?, 'https://cdn.test/issue-231.pdf', 'h1')",
            (FEED, ENTRY),
        )

    resp = main.entry_attachments_route(feed_url=FEED, entry_id=ENTRY)
    import json as _json
    data = _json.loads(resp.body.decode())

    assert [k["source_url"] for k in data["kept"]] == ["https://cdn.test/issue-231.pdf"]
    assert data["available"] == []


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
