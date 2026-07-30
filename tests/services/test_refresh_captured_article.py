"""Re-fetching a capture that has been filed onto a real feed.

Auto-filing moves a saved article out of `lectio:saved` and onto the feed that
actually publishes it. The article stays a Lectio capture (`added_by='user'`,
entry id = source URL) but both the Re-fetch route and its UI control used to
gate on feed identity, so filing silently stripped the re-fetch escape hatch
from every article the filer moved. These tests pin the in-place refresh that
replaces it — and that it never writes back into the saved feed, which would
resurrect the duplicate filing removed."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from services.reader_api import ReaderApi
from services.saved_articles import SAVED_FEED_URL, refresh_captured_article

REAL_FEED = "https://blog.example.com/feed/"
ARTICLE = "https://blog.example.com/topics/how-to-focus"


@pytest.fixture
def reader(tmp_path):
    r = ReaderApi(str(tmp_path / "reader.sqlite")).client()
    try:
        yield r
    finally:
        r.close()


@pytest.fixture
def meta_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE saved_entries (
            feed_url TEXT NOT NULL,
            entry_id TEXT NOT NULL,
            saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(feed_url, entry_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE entry_title_overrides (
            feed_url TEXT NOT NULL,
            entry_id TEXT NOT NULL,
            PRIMARY KEY(feed_url, entry_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE entry_content_overrides (
            feed_url TEXT NOT NULL,
            entry_id TEXT NOT NULL,
            content TEXT NOT NULL,
            PRIMARY KEY(feed_url, entry_id)
        )
        """
    )
    try:
        yield conn
    finally:
        conn.close()


def _extract_ok(url: str) -> tuple[str, str]:
    return "The Real Article", "<p>It's a familiar story.</p>"


def _extract_boom(url: str) -> tuple[str, str]:
    raise ValueError("fetch failed")


def _add_filed_capture(reader, meta_conn, *, feed=REAL_FEED, entry_id=ARTICLE):
    """A capture as it looks after auto-filing: user-added, on a real feed."""
    reader.add_feed(feed, allow_invalid_url=True, exist_ok=True)
    reader.disable_feed_updates(feed)
    reader.add_entry({
        "feed_url": feed,
        "id": entry_id,
        "link": entry_id,
        "title": "Stale Listing Page",
        "published": datetime.now(timezone.utc),
        "content": [{"value": "<p>By Jesse Will By Andrew Zaleski</p>"}],
    })
    meta_conn.execute(
        "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id) VALUES (?, ?)", (feed, entry_id)
    )
    meta_conn.commit()


def _stored_content(reader, feed, entry_id) -> str:
    row = reader._storage.get_db().execute(
        "SELECT content FROM entries WHERE feed = ? AND id = ?", (feed, entry_id)
    ).fetchone()
    return json.loads(row[0])[0]["value"]


def test_replaces_content_in_place_on_the_real_feed(reader, meta_conn):
    _add_filed_capture(reader, meta_conn)
    archived: list[tuple[str, str]] = []

    result = refresh_captured_article(
        reader, meta_conn, REAL_FEED, ARTICLE,
        extract=_extract_ok,
        enqueue_archive=lambda f, e: archived.append((f, e)),
    )

    assert result["ok"] is True
    assert result["refreshed"] is True
    assert result["title"] == "The Real Article"
    assert "familiar story" in _stored_content(reader, REAL_FEED, ARTICLE)
    assert archived == [(REAL_FEED, ARTICLE)]


def test_never_writes_into_the_saved_feed(reader, meta_conn):
    """Routing a filed article through the save path would re-create the
    Uncategorized copy that auto-filing removed."""
    _add_filed_capture(reader, meta_conn)

    refresh_captured_article(reader, meta_conn, REAL_FEED, ARTICLE, extract=_extract_ok)

    assert reader.get_entry((SAVED_FEED_URL, ARTICLE), None) is None
    saved_rows = meta_conn.execute(
        "SELECT feed_url FROM saved_entries WHERE entry_id = ?", (ARTICLE,)
    ).fetchall()
    assert [r["feed_url"] for r in saved_rows] == [REAL_FEED]


def test_updates_the_title_when_not_pinned(reader, meta_conn):
    _add_filed_capture(reader, meta_conn)

    refresh_captured_article(reader, meta_conn, REAL_FEED, ARTICLE, extract=_extract_ok)

    assert reader.get_entry((REAL_FEED, ARTICLE)).title == "The Real Article"


def test_a_pinned_title_survives_the_refresh(reader, meta_conn):
    """Edit title pins an override; a later re-fetch must not clobber it."""
    _add_filed_capture(reader, meta_conn)
    meta_conn.execute(
        "INSERT INTO entry_title_overrides (feed_url, entry_id) VALUES (?, ?)", (REAL_FEED, ARTICLE)
    )
    meta_conn.commit()

    refresh_captured_article(reader, meta_conn, REAL_FEED, ARTICLE, extract=_extract_ok)

    assert reader.get_entry((REAL_FEED, ARTICLE)).title == "Stale Listing Page"
    # ...but the content still refreshed.
    assert "familiar story" in _stored_content(reader, REAL_FEED, ARTICLE)


def test_refuses_an_unkept_feed_entry(reader, meta_conn):
    """A publisher's own entry that Lectio is not keeping gets re-written by the
    next feed refresh, so replacing its content would be silently undone.

    Kept entries (starred OR tagged) are allowed, because those pin the result —
    see test_tagged_feed_entry_is_refetched_and_pinned."""
    reader.add_feed(REAL_FEED, allow_invalid_url=True, exist_ok=True)
    reader.disable_feed_updates(REAL_FEED)
    reader.add_entry({
        "feed_url": REAL_FEED,
        "id": ARTICLE,
        "link": ARTICLE,
        "title": "Publisher Entry",
        "published": datetime.now(timezone.utc),
    })
    # add_entry marks it user-added; force the feed-provided case directly.
    db = reader._storage.get_db()
    db.execute(
        "UPDATE entries SET added_by = 'feed' WHERE feed = ? AND id = ?", (REAL_FEED, ARTICLE)
    )
    db.commit()

    result = refresh_captured_article(reader, meta_conn, REAL_FEED, ARTICLE, extract=_extract_ok)

    assert result["ok"] is False
    assert "captured" in result["error"]


def _add_starred_feed_entry(reader, meta_conn, *, published=None):
    """A feed-provided (added_by='feed') entry the user has starred."""
    reader.add_feed(REAL_FEED, allow_invalid_url=True, exist_ok=True)
    reader.disable_feed_updates(REAL_FEED)
    reader.add_entry({
        "feed_url": REAL_FEED, "id": ARTICLE, "link": ARTICLE, "title": "Feed Post",
        "published": published or datetime(2020, 1, 1, tzinfo=timezone.utc),
        "content": [{"value": "<p>Thin feed content, no images.</p>"}],
    })
    db = reader._storage.get_db()
    db.execute("UPDATE entries SET added_by = 'feed' WHERE feed = ? AND id = ?", (REAL_FEED, ARTICLE))
    db.commit()
    meta_conn.execute(
        "INSERT INTO saved_entries (feed_url, entry_id) VALUES (?, ?)", (REAL_FEED, ARTICLE)
    )
    meta_conn.commit()


def test_starred_feed_entry_is_refetched_and_pinned(reader, meta_conn):
    """A starred feed entry whose feed content is thin/imageless can be enriched;
    the fuller content is pinned so a later refresh can't clobber it."""
    _add_starred_feed_entry(reader, meta_conn)
    result = refresh_captured_article(reader, meta_conn, REAL_FEED, ARTICLE, extract=_extract_ok)
    assert result["ok"] is True
    assert "familiar story" in _stored_content(reader, REAL_FEED, ARTICLE)
    # Pinned:
    pin = meta_conn.execute(
        "SELECT content FROM entry_content_overrides WHERE feed_url = ? AND entry_id = ?",
        (REAL_FEED, ARTICLE),
    ).fetchone()
    assert pin is not None and "familiar story" in pin[0]


def test_starred_feed_entry_keeps_its_date(reader, meta_conn):
    """Unlike a capture, an enriched feed entry keeps its chronological position
    (no bump to the top)."""
    original = datetime(2020, 1, 1, tzinfo=timezone.utc)
    _add_starred_feed_entry(reader, meta_conn, published=original)
    refresh_captured_article(reader, meta_conn, REAL_FEED, ARTICLE, extract=_extract_ok)
    pub = reader.get_entry((REAL_FEED, ARTICLE)).published
    assert pub.year == 2020  # not bumped to now


def test_missing_entry_is_reported(reader, meta_conn):
    result = refresh_captured_article(reader, meta_conn, REAL_FEED, ARTICLE, extract=_extract_ok)
    assert result["ok"] is False
    assert result["error"] == "Entry not found."


def test_a_failed_fetch_leaves_the_stored_copy_alone(reader, meta_conn):
    """A bad capture is still better than an empty one."""
    _add_filed_capture(reader, meta_conn)

    result = refresh_captured_article(reader, meta_conn, REAL_FEED, ARTICLE, extract=_extract_boom)

    assert result["ok"] is False
    assert "By Jesse Will" in _stored_content(reader, REAL_FEED, ARTICLE)


def test_an_empty_extraction_leaves_the_stored_copy_alone(reader, meta_conn):
    _add_filed_capture(reader, meta_conn)

    result = refresh_captured_article(
        reader, meta_conn, REAL_FEED, ARTICLE, extract=lambda url: ("Title", "")
    )

    assert result["ok"] is False
    assert "By Jesse Will" in _stored_content(reader, REAL_FEED, ARTICLE)


class _FakeStatusError(Exception):
    """Stands in for httpx.HTTPStatusError: carries a .response.status_code the
    service duck-types, without importing httpx into the test."""
    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.response = type("R", (), {"status_code": status})()


@pytest.mark.parametrize("status", [404, 410])
def test_a_dead_source_is_reported_as_gone(reader, meta_conn, status):
    """A 404/410 means the article is gone at the source — re-fetch will never
    work, so the result says so and flags `dead` so the UI can offer to delete
    instead of leaving the user retrying a dead URL."""
    _add_filed_capture(reader, meta_conn)

    def _extract_gone(url: str):
        raise _FakeStatusError(status)

    result = refresh_captured_article(reader, meta_conn, REAL_FEED, ARTICLE, extract=_extract_gone)

    assert result["ok"] is False
    assert result["dead"] is True
    assert str(status) in result["error"] and "gone" in result["error"].lower()
    assert "By Jesse Will" in _stored_content(reader, REAL_FEED, ARTICLE)  # untouched


def test_a_transient_http_error_is_not_flagged_dead(reader, meta_conn):
    """A 503 (or any non-404/410) is not a dead source — surface the code but
    keep `dead` false so the UI doesn't offer to delete a retryable article."""
    _add_filed_capture(reader, meta_conn)

    def _extract_503(url: str):
        raise _FakeStatusError(503)

    result = refresh_captured_article(reader, meta_conn, REAL_FEED, ARTICLE, extract=_extract_503)

    assert result["ok"] is False
    assert result["dead"] is False
    assert "503" in result["error"]


def _tag_entry(reader, feed_url, entry_id, tag="python"):
    db = reader._storage.get_db()
    db.execute(
        "INSERT OR REPLACE INTO entry_tags (feed, id, key, value) VALUES (?, ?, ?, ?)",
        (feed_url, entry_id, "lectio.manual_tag." + tag, "null"),
    )
    db.commit()


def test_tagged_feed_entry_is_refetched_and_pinned(reader, meta_conn):
    """A TAGGED but unstarred feed entry must be re-fetchable.

    Tag-as-keep made a manual tag a keep signal everywhere else, and this gate
    never followed — so a tagged post appeared in the Saved view with no way to
    re-fetch its content. 14,695 items on the live library. Reported as "why is
    neither refetch available on this one?".
    """
    reader.add_feed(REAL_FEED, allow_invalid_url=True, exist_ok=True)
    reader.disable_feed_updates(REAL_FEED)
    reader.add_entry({
        "feed_url": REAL_FEED, "id": ARTICLE, "link": ARTICLE, "title": "Feed Post",
        "published": datetime(2020, 1, 1, tzinfo=timezone.utc),
        "content": [{"value": "<p>Thin feed content.</p>"}],
    })
    db = reader._storage.get_db()
    db.execute("UPDATE entries SET added_by = 'feed' WHERE feed = ? AND id = ?", (REAL_FEED, ARTICLE))
    db.commit()
    _tag_entry(reader, REAL_FEED, ARTICLE)          # tagged, NOT starred

    result = refresh_captured_article(reader, meta_conn, REAL_FEED, ARTICLE, extract=_extract_ok)

    assert result["ok"] is True
    # Pinned, so the next feed refresh can't clobber the fuller copy — which is
    # what makes allowing a feed entry safe at all.
    assert meta_conn.execute(
        "SELECT 1 FROM entry_content_overrides WHERE feed_url = ? AND entry_id = ?",
        (REAL_FEED, ARTICLE),
    ).fetchone() is not None


def test_a_parked_page_at_the_url_is_refused(reader, meta_conn):
    """⚠ A 200 does not mean the article is still there.

    the-digital-reader served a parked "Empowering Relationships" page for a 2019
    post, and the re-fetch replaced the stored article AND its title with it —
    destroying the only copy, because the archive was rewritten too.

    The reference is the URL's own SLUG, not the stored title: re-fetch exists
    partly to replace a bad capture's title, so comparing old against new would
    refuse the very case the feature is for. A slug does not change when a site
    starts serving a parked page over it.
    """
    url = ("https://the-digital-reader.com/2019/01/22/"
           "33-ornament-dingbat-and-other-decorative-fonts-for-your-next-ebook/")
    reader.add_feed(REAL_FEED, allow_invalid_url=True, exist_ok=True)
    reader.disable_feed_updates(REAL_FEED)
    reader.add_entry({
        "feed_url": REAL_FEED, "id": url, "link": url,
        "title": "33 Ornament, Dingbat and Other Decorative Fonts",
        "content": [{"value": "<p>The original article body.</p>"}],
    })
    meta_conn.execute("INSERT INTO saved_entries (feed_url, entry_id) VALUES (?, ?)",
                      (REAL_FEED, url))
    meta_conn.commit()

    def _parked(u):
        return "Empowering Relationships - The Digital Reader", "<p>Unrelated filler.</p>"

    result = refresh_captured_article(reader, meta_conn, REAL_FEED, url, extract=_parked)

    assert result["ok"] is False
    assert result.get("mismatch") is True
    entry = reader.get_entry((REAL_FEED, url))
    assert "original article body" in entry.content[0].value   # untouched
    assert entry.title.startswith("33 Ornament")


def test_the_guard_stands_down_when_it_cannot_judge():
    """Conservative by design: refusing a legitimate re-fetch is a nuisance, while
    accepting a wrong one destroys the stored copy. So it fires only on ZERO
    overlap, and not at all when the slug carries too little to judge."""
    from services.saved_articles import _page_is_a_different_article as different

    rich = ("https://the-digital-reader.com/2019/01/22/"
            "33-ornament-dingbat-and-other-decorative-fonts-for-your-next-ebook/")
    assert different(rich, "Empowering Relationships - The Digital Reader")
    assert not different(rich, "33 Ornament, Dingbat and Other Decorative Fonts")

    # Opaque id, date-only path, thin section slug: nothing to compare against.
    assert not different("https://blog.chrismoore.com/?p=1524", "Anything At All")
    assert not different("https://x.test/2019/01/22/", "Anything At All")
    assert not different("https://blog.example.com/topics/how-to-focus", "The Real Article")

    # A title that genuinely echoes its slug is never refused.
    assert not different("https://x.test/2020/09/08/stl-algorithms-tutorial-unique-copy",
                         "STL Algorithms Tutorial: unique_copy")


def test_query_string_urls_are_judged_on_their_query(reader, meta_conn):
    """informit.com defeated the path-only version of this guard.

    Its URLs are /articles/article.aspx?p=…&WT.rss_a=<the actual title>. The path
    is furniture, and worse, "articles" overlapped the site index title
    "Articles | InformIT" — so a wrong page read as the right one and the stored
    article was replaced with the section index.

    Query VALUES are now part of the reference, and structural vocabulary
    (article/index/aspx/…) is excluded from it.
    """
    from services.saved_articles import _page_is_a_different_article as different

    url = ("http://www.informit.com/articles/article.aspx?p=2432250&WT.rss_f=Article"
           "&WT.rss_a=Working%20with%20the%20PowerShell%20Desired%20State%20Configuration"
           "%2C%20Part%202%3A%20Implementation%20and%20Troubleshooting")

    assert different(url, "Articles | InformIT")
    assert not different(url, "Working with the PowerShell Desired State Configuration, Part 2")


def test_structural_url_words_are_not_evidence():
    """A path of nothing but CMS furniture leaves no reference, so the guard must
    stand down rather than match on "article" or "index"."""
    from services.saved_articles import _url_slug_words

    assert _url_slug_words("https://x.test/articles/article.aspx") == set()
    assert _url_slug_words("https://x.test/index.php") == set()
    assert "powershell" in _url_slug_words("https://x.test/a.aspx?t=PowerShell+Desired+State")
