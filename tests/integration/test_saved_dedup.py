"""Saved Articles duplicate handling.

Two sides:
  1. The automatic cross-feed dupe suppression (_cleanup_intra_feed_slug_dupes)
     must SKIP lectio:saved — a saved copy shares its link with the source
     feed's entry, and suppressing it would silently mark the saved copy read
     and bury it in the read-later backlog.
  2. The user-driven Saved scan (GET /saved/duplicates + POST /saved/deduplicate)
     finds the same article saved under different URLs and hard-deletes the
     copies the user confirms.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
from services import saved_articles as saved_articles_service
from services import tenancy

FEED_A = "https://blog.example.test/feed"
FEED_B = "https://planet.example.test/feed"
SAVED = saved_articles_service.SAVED_FEED_URL


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
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _add_entry(reader, feed_url: str, entry_id: str, *, link: str, title: str = "",
               published: datetime | None = None, content: str | None = None) -> None:
    entry: dict = {"feed_url": feed_url, "id": entry_id, "link": link, "title": title}
    if published is not None:
        entry["published"] = published
    if content is not None:
        entry["content"] = [{"value": content}]
    reader.add_entry(entry)


def _client() -> TestClient:
    app = FastAPI()
    app.get("/saved/duplicates")(main.get_saved_duplicates)
    app.post("/saved/deduplicate")(main.deduplicate_saved)
    app.post("/saved/duplicates/preview")(main.preview_saved_duplicates)
    return TestClient(app)


# ── automatic scan exclusion ──────────────────────────────────────────────────

def test_cross_feed_cleanup_skips_saved_articles(configured):
    link = "https://blog.example.test/my-great-article"
    with main.get_reader() as reader:
        reader.add_feed(FEED_A, exist_ok=True)
        _add_entry(reader, FEED_A, "e1", link=link,
                   published=datetime(2026, 1, 1, tzinfo=timezone.utc))
        saved_articles_service.ensure_saved_feed(reader)
        _add_entry(reader, SAVED, link, link=link,
                   published=datetime(2026, 7, 1, tzinfo=timezone.utc))
        with main.get_meta_connection() as conn:
            suppressed = main._cleanup_intra_feed_slug_dupes(reader, conn)
        assert suppressed == 0
        assert reader.get_entry((SAVED, link)).read is False
        assert reader.get_entry((FEED_A, "e1")).read is False


def test_cross_feed_cleanup_still_suppresses_regular_feeds(configured):
    link = "https://blog.example.test/my-great-article"
    with main.get_reader() as reader:
        for feed, when in ((FEED_A, datetime(2026, 1, 1, tzinfo=timezone.utc)),
                           (FEED_B, datetime(2026, 1, 2, tzinfo=timezone.utc))):
            reader.add_feed(feed, exist_ok=True)
            _add_entry(reader, feed, "e1", link=link, published=when)
        with main.get_meta_connection() as conn:
            suppressed = main._cleanup_intra_feed_slug_dupes(reader, conn)
        assert suppressed == 1
        assert reader.get_entry((FEED_A, "e1")).read is False  # oldest copy kept
        assert reader.get_entry((FEED_B, "e1")).read is True


# ── /saved/duplicates scan ────────────────────────────────────────────────────

def _seed_saved(reader) -> None:
    saved_articles_service.ensure_saved_feed(reader)
    # Confirmed pair: same slug, one URL carries a tracking param. The older
    # copy has extracted content, so it must be the keeper (index 0).
    _add_entry(reader, SAVED, "https://a.example.test/my-great-article",
               link="https://a.example.test/my-great-article",
               title="My Great Article",
               published=datetime(2026, 1, 1, tzinfo=timezone.utc),
               content="<p>" + "great content " * 10 + "</p>")
    _add_entry(reader, SAVED, "https://a.example.test/my-great-article?utm_source=ig",
               link="https://a.example.test/my-great-article?utm_source=ig",
               title="My Great Article",
               published=datetime(2026, 7, 1, tzinfo=timezone.utc))
    # Possible pair: same 4+ word title on two different domains/slugs.
    _add_entry(reader, SAVED, "https://b.example.test/reposted-piece",
               link="https://b.example.test/reposted-piece",
               title="Four Word Title Here",
               published=datetime(2026, 2, 1, tzinfo=timezone.utc))
    _add_entry(reader, SAVED, "https://c.example.test/original-piece",
               link="https://c.example.test/original-piece",
               title="Four Word Title Here",
               published=datetime(2026, 2, 2, tzinfo=timezone.utc))
    # Unrelated article — must not appear in any group.
    _add_entry(reader, SAVED, "https://d.example.test/something-else-entirely",
               link="https://d.example.test/something-else-entirely",
               title="Nothing To See Anywhere",
               published=datetime(2026, 3, 1, tzinfo=timezone.utc))


def test_saved_duplicates_scan_groups_and_tiers(configured):
    with main.get_reader() as reader:
        _seed_saved(reader)
    with _client() as c:
        r = c.get("/saved/duplicates")
    assert r.status_code == 200
    data = r.json()
    assert data["scanned"] == 5

    assert len(data["confirmed"]) == 1
    group = data["confirmed"][0]
    assert "same slug" in group["reasons"]
    ids = [e["entry_id"] for e in group["entries"]]
    # Keeper first: the copy WITH content, even though both share the slug.
    assert ids[0] == "https://a.example.test/my-great-article"
    assert ids[1] == "https://a.example.test/my-great-article?utm_source=ig"
    assert group["entries"][0]["has_content"] is True
    assert group["entries"][1]["has_content"] is False

    assert len(data["possible"]) == 1
    ptitles = {e["title"] for e in data["possible"][0]["entries"]}
    assert ptitles == {"Four Word Title Here"}
    assert "same title" in data["possible"][0]["reasons"]


def test_saved_duplicates_keeper_prefers_https_over_older_http(configured):
    http_url = "http://a.example.test/my-great-article"
    https_url = "https://a.example.test/my-great-article"
    with main.get_reader() as reader:
        saved_articles_service.ensure_saved_feed(reader)
        _add_entry(reader, SAVED, http_url, link=http_url, title="My Great Article",
                   published=datetime(2026, 1, 1, tzinfo=timezone.utc))
        _add_entry(reader, SAVED, https_url, link=https_url, title="My Great Article",
                   published=datetime(2026, 7, 1, tzinfo=timezone.utc))
    with _client() as c:
        r = c.get("/saved/duplicates")
    data = r.json()
    assert len(data["confirmed"]) == 1
    ids = [e["entry_id"] for e in data["confirmed"][0]["entries"]]
    assert ids == [https_url, http_url]  # https keeper despite the older http save


def test_saved_duplicates_content_beats_https(configured):
    http_url = "http://a.example.test/my-great-article"
    https_url = "https://a.example.test/my-great-article"
    with main.get_reader() as reader:
        saved_articles_service.ensure_saved_feed(reader)
        _add_entry(reader, SAVED, http_url, link=http_url, title="My Great Article",
                   published=datetime(2026, 1, 1, tzinfo=timezone.utc),
                   content="<p>" + "the only extracted copy " * 5 + "</p>")
        _add_entry(reader, SAVED, https_url, link=https_url, title="My Great Article",
                   published=datetime(2026, 7, 1, tzinfo=timezone.utc))
    with _client() as c:
        r = c.get("/saved/duplicates")
    data = r.json()
    ids = [e["entry_id"] for e in data["confirmed"][0]["entries"]]
    assert ids == [http_url, https_url]  # don't throw away the only copy with content


def test_saved_duplicates_same_body_lands_in_possible(configured):
    body = "<p>" + "identical extracted text " * 5 + "</p>"
    with main.get_reader() as reader:
        saved_articles_service.ensure_saved_feed(reader)
        _add_entry(reader, SAVED, "https://a.example.test/typo-titel-fixed",
                   link="https://a.example.test/typo-titel-fixed",
                   title="Ye Olde Typo Titel", content=body,
                   published=datetime(2026, 1, 1, tzinfo=timezone.utc))
        _add_entry(reader, SAVED, "https://a.example.test/typo-title-corrected",
                   link="https://a.example.test/typo-title-corrected",
                   title="The Corrected Title Version", content=body,
                   published=datetime(2026, 1, 2, tzinfo=timezone.utc))
    with _client() as c:
        r = c.get("/saved/duplicates")
    data = r.json()
    assert data["confirmed"] == []
    assert len(data["possible"]) == 1
    assert data["possible"][0]["reasons"] == ["same content"]


def test_saved_duplicates_empty_without_saved_feed(configured):
    with _client() as c:
        r = c.get("/saved/duplicates")
    assert r.json() == {"confirmed": [], "possible": [], "scanned": 0}


def test_saved_duplicates_preview_returns_stored_text(configured):
    with_content = "https://a.example.test/my-great-article"
    without = "https://a.example.test/my-great-article?utm_source=ig"
    with main.get_reader() as reader:
        _seed_saved(reader)
    with _client() as c:
        r = c.post("/saved/duplicates/preview",
                   json={"entry_ids": [with_content, without, "https://nope.example.test/gone"]})
    assert r.status_code == 200
    previews = r.json()["previews"]
    assert [p["entry_id"] for p in previews] == [with_content, without]  # unknown id skipped
    assert "great content" in previews[0]["text"]
    assert previews[0]["words"] > 0
    assert previews[0]["chars"] == len(previews[0]["text"])  # short body: untruncated
    assert previews[1]["text"] == "" and previews[1]["words"] == 0
    assert previews[0]["title"] == "My Great Article"


# ── /saved/deduplicate bulk delete ────────────────────────────────────────────

def test_saved_deduplicate_deletes_and_tombstones(configured):
    dupe = "https://a.example.test/my-great-article?utm_source=ig"
    keeper = "https://a.example.test/my-great-article"
    with main.get_reader() as reader:
        _seed_saved(reader)
    with main.get_meta_connection() as conn:
        conn.execute("INSERT OR IGNORE INTO saved_entries (feed_url, entry_id) VALUES (?, ?)",
                     (SAVED, dupe))
    with _client() as c:
        r = c.post("/saved/deduplicate",
                   json={"entry_ids": [dupe, "https://nope.example.test/missing"]})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "deleted": 1, "errors": 0}
    with main.get_reader() as reader:
        assert reader.get_entry((SAVED, dupe), None) is None
        assert reader.get_entry((SAVED, keeper), None) is not None
    with main.get_meta_connection() as conn:
        assert conn.execute(
            "SELECT 1 FROM deleted_entries WHERE feed_url = ? AND entry_id = ?",
            (SAVED, dupe)).fetchone()
        assert not conn.execute(
            "SELECT 1 FROM saved_entries WHERE feed_url = ? AND entry_id = ?",
            (SAVED, dupe)).fetchone()
