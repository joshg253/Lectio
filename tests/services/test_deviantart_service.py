"""Tests for the DeviantArt synthetic-feed service."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from services import deviantart as da


@pytest.fixture(autouse=True)
def _init_dir(tmp_path):
    da.init(tmp_path)
    da._token_cache.clear()
    yield


# --- URL parsing ---

@pytest.mark.parametrize("url, expected", [
    ("https://backend.deviantart.com/rss.xml?q=gallery:arcipello&type=deviation", "arcipello"),
    ("https://backend.deviantart.com/rss.xml?q=gallery:red-j&type=deviation", "red-j"),
    ("https://www.deviantart.com/yuumei", "yuumei"),
    ("https://www.deviantart.com/koyorin/gallery/all", "koyorin"),
    ("https://patrickbrown.deviantart.com/", "patrickbrown"),
    ("https://example.com/feed", None),
    ("https://www.deviantart.com/settings", None),
    ("https://backend.deviantart.com/rss.xml?q=tag:cats", None),
])
def test_username_from_url(url, expected):
    assert da.username_from_url(url) == expected


def test_is_deviantart_url():
    assert da.is_deviantart_url("https://www.deviantart.com/yuumei")
    assert not da.is_deviantart_url("https://example.com")


# --- file-url dir-awareness ---

def test_feed_id_from_url_roundtrip():
    fid = "abc-123"
    url = da.feed_file_url(fid)
    assert da.deviantart_feed_id_from_url(url) == fid


def test_feed_id_from_url_rejects_other_dirs():
    assert da.deviantart_feed_id_from_url("file:///some/other/dir/xyz.xml") is None
    assert da.deviantart_feed_id_from_url("https://example.com/x") is None


# --- deviation mapping + RSS ---

def test_deviation_to_entry_embeds_image():
    dev = {
        "deviationid": "DID1",
        "url": "https://www.deviantart.com/x/art/Y-1",
        "title": "My <Art>",
        "published_time": "1700000000",
        "content": {"src": "https://images/x.jpg"},
        "author": {"username": "x"},
        "is_mature": False,
    }
    e = da._deviation_to_entry(dev)
    assert e is not None
    assert e["id"] == "DID1"
    assert e["title"] == "My <Art>"
    assert "images/x.jpg" in e["content"] and "<img" in e["content"]
    assert e["published_at"].startswith("2023-11-14")


def test_deviation_to_entry_falls_back_to_thumb():
    dev = {
        "deviationid": "D2", "url": "https://da/y", "title": "t",
        "published_time": "x", "thumbs": [{"src": "https://t/small.jpg"}],
    }
    e = da._deviation_to_entry(dev)
    assert e is not None
    assert "small.jpg" in e["content"]


def test_deviation_to_entry_skips_without_id_or_url():
    assert da._deviation_to_entry({"url": "https://da/y"}) is None
    assert da._deviation_to_entry({"deviationid": "z"}) is None


def test_generate_rss_xml_structure():
    e = {"id": "g1", "title": "Title & <stuff>", "entry_url": "https://da/p",
         "content": "<p>hi</p>", "published_at": "2024-01-01T00:00:00+00:00"}
    xml = da._generate_rss_xml("Feed", "https://src", [e])
    assert '<?xml version="1.0"' in xml
    assert "<rss version=\"2.0\">" in xml
    assert "guid" in xml and "g1" in xml


# --- token + gallery fetch (mocked HTTP) ---

def _mock_client(responses):
    """responses: list of (status_code, json) returned in order for post/get."""
    calls = {"i": 0}

    def _resp(status, payload):
        r = MagicMock()
        r.status_code = status
        r.json.return_value = payload
        r.text = str(payload)
        return r

    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False

    def _next(*a, **k):
        idx = calls["i"]
        calls["i"] += 1
        status, payload = responses[idx]
        return _resp(status, payload)

    client.post.side_effect = _next
    client.get.side_effect = _next
    client.request.side_effect = _next  # used by the 429-retrying _request helper
    return client


def test_get_token_caches():
    client = _mock_client([(200, {"access_token": "TOK", "expires_in": 3600})])
    with patch("httpx.Client", return_value=client):
        assert da._get_token("cid", "sec") == "TOK"
        # Second call hits cache → no new HTTP call.
        assert da._get_token("cid", "sec") == "TOK"
    assert client.post.call_count == 1


def test_verify_credentials_ok_and_fail():
    ok_client = _mock_client([(200, {"access_token": "T", "expires_in": 3600})])
    with patch("httpx.Client", return_value=ok_client):
        ok, _ = da.verify_credentials("cid", "sec")
    assert ok

    # invalid_client → public-client guidance (key validation not available).
    bad_client = _mock_client([(401, {"error": "invalid_client"})])
    with patch("httpx.Client", return_value=bad_client):
        ok, msg = da.verify_credentials("cid", "bad")
    assert not ok and "Connect" in msg

    # A non-auth error still surfaces its detail.
    err_client = _mock_client([(500, {"error": "server_error"})])
    with patch("httpx.Client", return_value=err_client):
        ok, msg = da.verify_credentials("cid", "x")
    assert not ok and "500" in msg


def test_verify_credentials_requires_both():
    ok, _ = da.verify_credentials("", "x")
    assert not ok


def test_fetch_gallery_paginates():
    responses = [
        (200, {"access_token": "T", "expires_in": 3600}),  # token
        (200, {"results": [{"deviationid": f"d{i}"} for i in range(24)], "has_more": True, "next_offset": 24}),
        (200, {"results": [{"deviationid": "d24"}], "has_more": False}),
    ]
    client = _mock_client(responses)
    with patch("httpx.Client", return_value=client):
        out = da.fetch_gallery("cid", "sec", "someuser", limit=50)
    assert len(out) == 25
    assert out[0]["deviationid"] == "d0"


# --- OAuth2 authorization_code (Phase 2) ---

def test_generate_pkce_pair():
    v, c = da.generate_pkce_pair()
    assert 43 <= len(v) <= 128
    assert c and "=" not in c and "+" not in c and "/" not in c  # base64url, no padding


def test_authorize_url_has_required_params():
    url = da.authorize_url("CID", "https://host/deviantart/callback", "STATE123", "CHALLENGE")
    assert url.startswith("https://www.deviantart.com/oauth2/authorize?")
    assert "response_type=code" in url
    assert "client_id=CID" in url
    assert "state=STATE123" in url
    assert "redirect_uri=https%3A%2F%2Fhost%2Fdeviantart%2Fcallback" in url
    assert "user.manage" in url  # scope present (url-encoded space)
    assert "code_challenge=CHALLENGE" in url
    assert "code_challenge_method=S256" in url


def test_exchange_code_returns_tokens():
    data = _call_with_json_ct(da.exchange_code, "cid", "sec", "code", "https://host/cb", "verifier")
    assert data["access_token"] == "A" and data["refresh_token"] == "R"


def test_list_watching_paginates():
    responses = [
        (200, {"results": [{"user": {"username": "a"}}, {"user": {"username": "b"}}], "has_more": True, "next_offset": 50}),
        (200, {"results": [{"user": {"username": "c"}}], "has_more": False}),
    ]
    client = _mock_client(responses)
    with patch("httpx.Client", return_value=client):
        names = da.list_watching("TOKEN", "me")
    assert names == ["a", "b", "c"]


def test_whoami_returns_username():
    client = _mock_client([(200, {"username": "me"})])
    with patch("httpx.Client", return_value=client):
        assert da.whoami("TOKEN") == "me"


# --- rate-limit handling (_request / DeviantArtRateLimited) ---

def test_request_raises_rate_limited_after_max_retries(monkeypatch):
    monkeypatch.setattr(da.time, "sleep", lambda *a, **k: None)  # no real backoff
    client = _mock_client([(429, {})] * da._MAX_RETRIES)
    with patch("httpx.Client", return_value=client) as mk:
        with pytest.raises(da.DeviantArtRateLimited):
            da._request("GET", "https://api/x", headers={})
    assert client.request.call_count == da._MAX_RETRIES
    assert mk.call_count == 1  # one client reused across all retries, not per-attempt


def test_request_returns_after_429_then_success(monkeypatch):
    monkeypatch.setattr(da.time, "sleep", lambda *a, **k: None)
    client = _mock_client([(429, {}), (200, {"ok": True})])
    with patch("httpx.Client", return_value=client) as mk:
        resp = da._request("GET", "https://api/x", headers={})
    assert resp.status_code == 200
    assert client.request.call_count == 2
    assert mk.call_count == 1


def _da_conn_with_feeds(feed_ids):
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE deviantart_feeds (id TEXT PRIMARY KEY, username TEXT,"
        " feed_title TEXT, created_at TEXT, last_synced_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE deviantart_entries (id TEXT PRIMARY KEY, deviantart_feed_id TEXT,"
        " deviationid TEXT, title TEXT, entry_url TEXT, content TEXT, published_at TEXT, tags TEXT NOT NULL DEFAULT '',"
        " UNIQUE(deviantart_feed_id, deviationid))"
    )
    for fid in feed_ids:
        conn.execute("INSERT INTO deviantart_feeds VALUES (?, 'u', 't', 'now', NULL)", (fid,))
    return conn


def test_refresh_all_stops_on_rate_limit(monkeypatch):
    """A 429 mid-batch must halt the cycle (not raise, not churn the remaining feeds)."""
    conn = _da_conn_with_feeds(["f0", "f1", "f2"])
    calls = {"n": 0}

    def _boom(*a, **k):
        calls["n"] += 1
        raise da.DeviantArtRateLimited("limit")

    monkeypatch.setattr(da, "refresh_deviantart_feed_by_id", _boom)
    da.refresh_all_deviantart_feeds(conn, "cid", "sec", access_token="T")  # must not raise
    assert calls["n"] == 1  # stopped after the first feed hit the limit


# --- _upsert_entries lead-image seeding ---

def test_upsert_entries_seeds_lead_image_sink():
    conn = _da_conn_with_feeds(["FID"])
    sink_calls = []
    da.set_lead_image_sink(lambda *a: sink_calls.append(a))
    try:
        devs = [{"deviationid": "D1", "url": "https://da/p1", "title": "t1",
                 "published_time": "1700000000", "content": {"src": "https://img/1.jpg"}}]
        added = da._upsert_entries(conn, "FID", devs)
    finally:
        da.set_lead_image_sink(None)
    assert added == 1
    assert sink_calls == [(da.feed_file_url("FID"), "D1", "https://img/1.jpg")]


def test_upsert_entries_no_sink_when_no_image():
    conn = _da_conn_with_feeds(["FID"])
    sink_calls = []
    da.set_lead_image_sink(lambda *a: sink_calls.append(a))
    try:
        devs = [{"deviationid": "D2", "url": "https://da/p2", "title": "t2",
                 "published_time": "1700000000"}]  # no content/thumbs -> empty image_src
        added = da._upsert_entries(conn, "FID", devs)
    finally:
        da.set_lead_image_sink(None)
    assert added == 1
    assert sink_calls == []


# --- _post_token: public vs confidential client ---

def _token_client(responses):
    """Like _mock_client but every response reports a JSON content-type, which
    _post_token requires before reading the body."""
    calls = {"i": 0}

    def _resp(status, payload):
        r = MagicMock()
        r.status_code = status
        r.json.return_value = payload
        r.text = str(payload)
        r.headers = {"content-type": "application/json"}
        return r

    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False

    def _next(*a, **k):
        idx = calls["i"]
        calls["i"] += 1
        return _resp(*responses[idx])

    client.post.side_effect = _next
    return client


def test_post_token_sends_secret_first_for_confidential():
    client = _token_client([(200, {"access_token": "A", "refresh_token": "R"})])
    with patch("httpx.Client", return_value=client):
        data = da._post_token({"grant_type": "refresh_token", "client_id": "c"}, "SECRET", "refresh")
    assert data["access_token"] == "A"
    assert client.post.call_count == 1
    assert client.post.call_args_list[0].kwargs["data"].get("client_secret") == "SECRET"


def test_post_token_falls_back_to_public_client_without_secret():
    client = _token_client([(401, {"error": "invalid_client"}), (200, {"access_token": "A"})])
    with patch("httpx.Client", return_value=client):
        data = da._post_token({"grant_type": "refresh_token", "client_id": "c"}, "SECRET", "refresh")
    assert data["access_token"] == "A"
    assert client.post.call_count == 2
    assert "client_secret" not in client.post.call_args_list[1].kwargs["data"]


def test_post_token_raises_when_all_attempts_fail():
    client = _token_client([(401, {"error": "invalid_client"}), (400, {"error": "bad_grant"})])
    with patch("httpx.Client", return_value=client):
        with pytest.raises(RuntimeError):
            da._post_token({"grant_type": "refresh_token", "client_id": "c"}, "SECRET", "refresh")


def test_refresh_access_token_returns_new_tokens():
    client = _token_client([(200, {"access_token": "NEW", "refresh_token": "R2", "expires_in": 3600})])
    with patch("httpx.Client", return_value=client):
        data = da.refresh_access_token("cid", "sec", "oldrefresh")
    assert data["access_token"] == "NEW" and data["refresh_token"] == "R2"


def _call_with_json_ct(fn, *args):
    """Run an httpx-using fn where responses report JSON content-type."""
    import httpx  # noqa

    def _resp(status, payload):
        r = MagicMock()
        r.status_code = status
        r.json.return_value = payload
        r.text = str(payload)
        r.headers = {"content-type": "application/json"}
        return r

    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    client.post.return_value = _resp(200, {"access_token": "A", "refresh_token": "R", "expires_in": 3600})
    client.get.return_value = _resp(200, {"username": "me"})
    with patch("httpx.Client", return_value=client):
        return fn(*args)


def test_deviation_to_entry_carries_tags_when_present():
    dev = {"deviationid": "d1", "url": "https://da/x", "title": "T",
           "tags": [{"tag_name": "fantasy"}, "landscape"]}
    e = da._deviation_to_entry(dev)
    assert e is not None
    assert e["tags"] == ["fantasy", "landscape"]


def test_deviation_to_entry_no_tags_field():
    e = da._deviation_to_entry({"deviationid": "d1", "url": "https://da/x"})
    assert e is not None
    assert e["tags"] == []


def test_item_xml_emits_category_per_tag():
    e = {"id": "d1", "title": "T", "entry_url": "https://da/x", "content": "",
         "published_at": "2026-07-01T00:00:00+00:00", "tags": ["fantasy"]}
    assert "<category>fantasy</category>" in da._item_xml(e)


# --- deviation tags via /deviation/metadata ---

def test_fetch_deviation_tags_batches_and_maps():
    # 60 ids → two metadata calls of ≤50 (plus the initial token call).
    ids = [f"d{i}" for i in range(60)]
    meta = lambda batch: {"metadata": [
        {"deviationid": d, "tags": [{"tag_name": f"tag-{d}"}]} for d in batch
    ]}
    responses = [
        (200, {"access_token": "T", "expires_in": 3600}),
        (200, meta(ids[:50])),
        (200, meta(ids[50:])),
    ]
    client = _mock_client(responses)
    with patch("httpx.Client", return_value=client):
        out = da.fetch_deviation_tags("cid", "sec", ids)
    assert len(out) == 60
    assert out["d0"] == ["tag-d0"]
    # token (post) + 2 metadata requests
    assert client.request.call_count == 2


def _tags_conn():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE deviantart_entries (
        id TEXT PRIMARY KEY, deviantart_feed_id TEXT, deviationid TEXT,
        title TEXT, entry_url TEXT, content TEXT, published_at TEXT,
        tags TEXT NOT NULL DEFAULT '', tags_fetched_at TEXT,
        UNIQUE(deviantart_feed_id, deviationid))""")
    for i in range(3):
        conn.execute(
            "INSERT INTO deviantart_entries (id, deviantart_feed_id, deviationid, title, published_at)"
            " VALUES (?, 'f1', ?, 't', ?)", (f"row{i}", f"d{i}", f"2026-07-0{i+1}"))
    return conn


def test_fetch_and_store_missing_tags_updates_rows_and_marks_checked():
    conn = _tags_conn()
    with patch.object(da, "fetch_deviation_tags",
                      return_value={"d2": ["fantasy", "dragon"], "d1": []}) as fetched:
        tagged = da.fetch_and_store_missing_tags(conn, "f1", "cid", "sec")
    assert tagged == 1
    rows = {r["deviationid"]: r for r in conn.execute("SELECT * FROM deviantart_entries")}
    assert rows["d2"]["tags"] == "fantasy,dragon"
    # Zero-tag result still marked checked, so it isn't re-looked-up forever.
    assert rows["d1"]["tags"] == "" and rows["d1"]["tags_fetched_at"]
    assert rows["d0"]["tags_fetched_at"]
    # Second pass: nothing left to look up.
    with patch.object(da, "fetch_deviation_tags", return_value={}) as second:
        assert da.fetch_and_store_missing_tags(conn, "f1", "cid", "sec") == 0
    second.assert_not_called()


def test_fetch_and_store_missing_tags_survives_rate_limit():
    conn = _tags_conn()
    with patch.object(da, "fetch_deviation_tags", side_effect=da.DeviantArtRateLimited("quota")):
        assert da.fetch_and_store_missing_tags(conn, "f1", "cid", "sec") == 0
    # Nothing marked checked — the lookup retries on a later cycle.
    row = conn.execute("SELECT COUNT(*) FROM deviantart_entries WHERE tags_fetched_at IS NOT NULL").fetchone()
    assert row[0] == 0
