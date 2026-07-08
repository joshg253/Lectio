"""Tests for the dev.to filtered synthetic-feed service."""
from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from services import devto

F1 = "11111111-1111-4111-8111-111111111111"
F2 = "22222222-2222-4222-8222-222222222222"
F3 = "33333333-3333-4333-8333-333333333333"


@pytest.fixture(autouse=True)
def _init_dir(tmp_path):
    devto.init(tmp_path)
    yield


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE devto_feeds (id TEXT PRIMARY KEY, feed_title TEXT, tag TEXT,"
        " top_days INTEGER, english_only INTEGER NOT NULL DEFAULT 1, min_reactions INTEGER,"
        " tags_exclude TEXT, created_at TEXT, last_synced_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE devto_entries (id TEXT PRIMARY KEY, devto_feed_id TEXT,"
        " article_id TEXT, title TEXT, entry_url TEXT, content TEXT, published_at TEXT,"
        " UNIQUE(devto_feed_id, article_id))"
    )
    return conn


def _article(**over):
    a = {
        "id": 101,
        "title": "A Post",
        "url": "https://dev.to/u/a-post",
        "description": "About things.",
        "language": "en",
        "positive_reactions_count": 12,
        "comments_count": 3,
        "reading_time_minutes": 4,
        "tag_list": ["python", "tutorial"],
        "cover_image": "https://media.dev.to/cover.png",
        "published_timestamp": "2026-07-01T10:00:00Z",
        "user": {"name": "Ada"},
    }
    a.update(over)
    return a


# --- URL parsing ---

@pytest.mark.parametrize("url, expected", [
    ("https://dev.to/", {"tag": None}),
    ("https://dev.to", {"tag": None}),
    ("https://dev.to/feed", {"tag": None}),
    ("https://dev.to/t/python", {"tag": "python"}),
    ("https://dev.to/t/Python/top/week", {"tag": "python"}),
    ("https://dev.to/feed/tag/cpp", {"tag": "cpp"}),
    ("https://www.dev.to/t/csharp", {"tag": "csharp"}),
    ("https://dev.to/someuser", None),          # user pages keep normal RSS
    ("https://dev.to/feed/someuser", None),     # user feed too
    ("https://example.com/t/python", None),
    ("not a url", None),
])
def test_parse_devto_url(url, expected):
    assert devto.parse_devto_url(url) == expected


# --- file-url dir-awareness ---

def test_feed_id_from_url_roundtrip():
    fid = "abc-123"
    url = devto.feed_file_url(fid)
    assert devto.devto_feed_id_from_url(url) == fid


def test_feed_id_from_url_rejects_other_dirs():
    assert devto.devto_feed_id_from_url("file:///some/other/dir/xyz.xml") is None
    assert devto.devto_feed_id_from_url("https://example.com/x") is None


# --- param building + client-side filters ---

def test_build_params_full_config():
    params = devto._build_params({
        "tag": " Python ", "top_days": 7, "tags_exclude": "WebDev, career ,",
    })
    assert params == {"per_page": 80, "tag": "python", "top": 7, "tags_exclude": "webdev,career"}


def test_build_params_front_page_defaults():
    assert devto._build_params({}) == {"per_page": 80}


def test_filters_english_only():
    cfg = {"english_only": True}
    assert devto._passes_filters(_article(language="en"), cfg)
    assert not devto._passes_filters(_article(language="es"), cfg)
    assert not devto._passes_filters(_article(language=None), cfg)
    assert devto._passes_filters(_article(language="es"), {"english_only": False})


def test_filters_min_reactions():
    cfg = {"min_reactions": 10}
    assert devto._passes_filters(_article(positive_reactions_count=12), cfg)
    assert not devto._passes_filters(_article(positive_reactions_count=9), cfg)
    assert devto._passes_filters(_article(positive_reactions_count=0), {})


# --- article mapping + RSS ---

def test_article_to_entry_embeds_cover_and_meta():
    e = devto._article_to_entry(_article())
    assert e is not None
    assert e["id"] == "101"
    assert e["image_src"] == "https://media.dev.to/cover.png"
    assert "<img" in e["content"] and "cover.png" in e["content"]
    assert "Ada" in e["content"] and "12 reactions" in e["content"]
    assert "#python" in e["content"]
    assert e["published_at"].startswith("2026-07-01")


def test_article_to_entry_no_cover():
    e = devto._article_to_entry(_article(cover_image=None))
    assert e is not None
    assert e["image_src"] in ("", None) or not e["image_src"]
    assert "<img" not in e["content"]


def test_article_to_entry_skips_without_id_or_url():
    assert devto._article_to_entry({"url": "https://dev.to/x"}) is None
    assert devto._article_to_entry({"id": 5}) is None


def test_generate_rss_xml_structure():
    e = {"id": "101", "title": "Title & <stuff>", "entry_url": "https://dev.to/p",
         "content": "<p>hi</p>", "published_at": "2026-07-01T00:00:00+00:00"}
    xml = devto._generate_rss_xml("dev.to #python", "https://dev.to/t/python", [e])
    assert '<?xml version="1.0"' in xml
    assert '<rss version="2.0">' in xml
    assert "guid" in xml and "101" in xml


def test_default_title():
    assert devto.default_title({}) == "dev.to"
    assert devto.default_title({"tag": "python"}) == "dev.to #python"
    t = devto.default_title({"tag": "cpp", "top_days": 7, "min_reactions": 10})
    assert "cpp" in t and "7d" in t and "10" in t


# --- HTTP (mocked) ---

def _mock_client(responses):
    """responses: list of (status_code, payload, headers) returned in order."""
    calls = {"i": 0}

    def _resp(status, payload, headers):
        r = MagicMock()
        r.status_code = status
        r.json.return_value = payload
        r.text = str(payload)
        r.headers = headers or {}
        return r

    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False

    def _next(*a, **k):
        idx = calls["i"]
        calls["i"] += 1
        status, payload, headers = responses[idx]
        return _resp(status, payload, headers)

    client.get.side_effect = _next
    return client


def test_fetch_articles_filters_client_side():
    articles = [
        _article(id=1),
        _article(id=2, language="pt"),
        _article(id=3, positive_reactions_count=1),
    ]
    with patch("services.devto.httpx.Client", return_value=_mock_client([(200, articles, {})])):
        out = devto.fetch_articles({"tag": "python", "english_only": True, "min_reactions": 5})
    assert [a["id"] for a in out] == [1]


def test_request_raises_rate_limited_after_max_retries():
    responses = [(429, {}, {"Retry-After": "0"})] * devto._MAX_RETRIES
    with patch("services.devto.httpx.Client", return_value=_mock_client(responses)):
        with patch("services.devto.time.sleep"):
            with pytest.raises(devto.DevToRateLimited):
                devto._request(devto._API_URL, params={})


def test_request_returns_after_429_then_success():
    responses = [(429, {}, {"Retry-After": "0"}), (200, [], {})]
    with patch("services.devto.httpx.Client", return_value=_mock_client(responses)):
        with patch("services.devto.time.sleep"):
            resp = devto._request(devto._API_URL, params={})
    assert resp.status_code == 200


# --- upsert + lifecycle ---

def test_upsert_entries_idempotent_and_seeds_lead_image():
    conn = _db()
    conn.execute(
        "INSERT INTO devto_feeds (id, feed_title, tag, created_at) VALUES (?, 't', 'python', 'now')",
        (F1,),
    )
    seeded = []
    devto.set_lead_image_sink(lambda feed_url, entry_id, img: seeded.append((entry_id, img)))
    try:
        added = devto._upsert_entries(conn, F1, [_article(id=1), _article(id=2, cover_image=None)])
        assert added == 2
        assert devto._upsert_entries(conn, F1, [_article(id=1)]) == 0
        # The sink fires on every upsert pass (main.py's sink dedupes against
        # the lead-image cache); only the cover-image article is seeded.
        assert seeded == [("1", "https://media.dev.to/cover.png")] * 2
    finally:
        devto.set_lead_image_sink(None)


def test_refresh_all_stops_on_rate_limit():
    conn = _db()
    for i in range(3):
        conn.execute(
            "INSERT INTO devto_feeds (id, feed_title, created_at, last_synced_at)"
            " VALUES (?, 't', 'now', ?)", ([F1, F2, F3][i], f"2026-01-0{i + 1}"),
        )
    calls = []

    def _boom(_conn, fid):
        calls.append(fid)
        raise devto.DevToRateLimited("nope")

    with patch("services.devto.refresh_devto_feed_by_id", side_effect=_boom):
        devto.refresh_all_devto_feeds(conn)
    assert calls == [F1]


def test_update_config_refetches_and_rewrites(tmp_path):
    conn = _db()
    conn.execute(
        "INSERT INTO devto_feeds (id, feed_title, tag, english_only, created_at, last_synced_at)"
        " VALUES (?, 'dev.to #python', 'python', 1, 'now', 'now')",
        (F1,),
    )
    reader = MagicMock()
    with patch("services.devto.fetch_articles", return_value=[_article(id=9)]) as fetch:
        devto.update_devto_feed_config(conn, reader, F1, {
            "tag": "python", "top_days": 7, "english_only": True, "min_reactions": 10,
        })
    row = conn.execute("SELECT * FROM devto_feeds WHERE id = ?", (F1,)).fetchone()
    assert row["top_days"] == 7 and row["min_reactions"] == 10
    # Auto-generated title follows the new filters.
    assert row["feed_title"] == "dev.to #python (top 7d, ≥10 reactions)"
    assert fetch.call_args[0][0]["top_days"] == 7
    assert (devto._dir() / f"{F1}.xml").exists()
    reader.update_feed.assert_called_once()


def test_update_config_preserves_custom_title(tmp_path):
    conn = _db()
    conn.execute(
        "INSERT INTO devto_feeds (id, feed_title, tag, english_only, created_at, last_synced_at)"
        " VALUES (?, 'My Python Picks', 'python', 1, 'now', 'now')",
        (F1,),
    )
    with patch("services.devto.fetch_articles", return_value=[]):
        devto.update_devto_feed_config(conn, MagicMock(), F1, {
            "tag": "python", "top_days": 7, "english_only": True,
        })
    row = conn.execute("SELECT feed_title FROM devto_feeds WHERE id = ?", (F1,)).fetchone()
    assert row["feed_title"] == "My Python Picks"


def test_delete_devto_feed_removes_row_and_file():
    conn = _db()
    conn.execute("INSERT INTO devto_feeds (id, feed_title, created_at) VALUES (?, 't', 'now')", (F1,))
    conn.execute(
        "INSERT INTO devto_entries (id, devto_feed_id, article_id, title, published_at)"
        " VALUES ('e1', ?, '1', 't', 'now')", (F1,),
    )
    (devto._dir() / f"{F1}.xml").write_text("<rss/>")
    reader = MagicMock()
    devto.delete_devto_feed(conn, reader, F1)
    assert conn.execute("SELECT COUNT(*) FROM devto_feeds").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM devto_entries").fetchone()[0] == 0
    assert not (devto._dir() / f"{F1}.xml").exists()
    reader.delete_feed.assert_called_once()


def test_article_to_entry_carries_tags():
    e = devto._article_to_entry(_article())
    assert e is not None
    assert e["tags"] == ["python", "tutorial"]
    e2 = devto._article_to_entry(_article(tag_list=None))
    assert e2 is not None and e2["tags"] == []


def test_item_xml_emits_category_per_tag():
    e = {"id": "101", "title": "T", "entry_url": "https://dev.to/p",
         "content": "<p>hi</p>", "published_at": "2026-07-01T00:00:00+00:00",
         "tags": ["python", "a&b"]}
    xml = devto._item_xml(e)
    assert "<category>python</category>" in xml
    assert "<category>a&amp;b</category>" in xml


def test_item_xml_no_tags_no_category():
    e = {"id": "101", "title": "T", "entry_url": "https://dev.to/p",
         "content": "", "published_at": "2026-07-01T00:00:00+00:00"}
    assert "<category>" not in devto._item_xml(e)


def test_categories_flow_into_tag_sink_at_ingest():
    """End to end: generated synthetic RSS parsed by the sanitizing parser
    delivers the article tags to the entry tag sink (entry_feed_tags)."""
    import io

    from services import reader_sanitize
    from services.reader_sanitize import SanitizingFeedparserParser

    e = devto._article_to_entry(_article())
    xml = devto._generate_rss_xml("dev.to #python", "https://dev.to/t/python", [e])

    captured = []
    reader_sanitize.set_entry_tag_sink(lambda url, pairs: captured.append(pairs))
    try:
        SanitizingFeedparserParser()("file:///x/devto.xml", io.BytesIO(xml.encode()), {})
    finally:
        reader_sanitize.set_entry_tag_sink(None)
    assert captured == [[("101", ["python", "tutorial"])]]
