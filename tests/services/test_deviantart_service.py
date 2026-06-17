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
