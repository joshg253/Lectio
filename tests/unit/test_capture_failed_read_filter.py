"""`capture_failed` is a Saved-scope read filter: it survives normalization and cycles back to `all`."""

import main


def test_capture_failed_is_a_known_read_filter():
    assert main.normalize_read_filter("capture_failed") == "capture_failed"


def test_capture_failed_cycles_back_to_all():
    assert main.get_next_read_filter("capture_failed") == "all"


def test_orphan_merge_narrows_to_failed_captures(monkeypatch):
    orphans = [
        {"feed_url": "f", "id": "bad", "title": "t", "link": "l", "feed_title": "ft"},
        {"feed_url": "f", "id": "fine", "title": "t", "link": "l", "feed_title": "ft"},
    ]
    monkeypatch.setattr(main.starred_archive_service, "get_orphan_saved_entries", lambda *a, **k: orphans)
    monkeypatch.setattr(main.starred_archive_service, "get_capture_failed_keys", lambda: {("f", "bad")})

    def merged(**kw):
        return main.merge_orphan_saved_entries([], live_feed_urls=set(), sort_by="post", sort_dir="desc", limit=50, **kw)

    assert len(merged()) == 2
    assert [p["id"] for p in merged(capture_failed=True)] == ["bad"]
