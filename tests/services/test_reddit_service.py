"""Tests for the Reddit OAuth/feed service."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from services import reddit as svc


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url, expected", [
    ("https://old.reddit.com/r/technology/.rss", "technology"),
    ("https://www.reddit.com/r/buildapcsales/.rss", "buildapcsales"),
    ("https://old.reddit.com/r/deals/new/.rss", "deals"),
    ("https://old.reddit.com/user/spez/submitted/.rss", None),
    ("https://example.com/feed", None),
])
def test_subreddit_from_feed_url(url, expected):
    assert svc.subreddit_from_feed_url(url) == expected


@pytest.mark.parametrize("url, expected", [
    ("https://old.reddit.com/user/spez/submitted/.rss", "spez"),
    ("https://www.reddit.com/user/automoderator/submitted/", "automoderator"),
    ("https://old.reddit.com/r/technology/.rss", None),
])
def test_redditor_from_feed_url(url, expected):
    assert svc.redditor_from_feed_url(url) == expected


@pytest.mark.parametrize("url, is_reddit", [
    ("https://old.reddit.com/r/technology/.rss", True),
    ("https://www.reddit.com/r/buildapcsales/.rss", True),
    ("https://example.com/feed", False),
    ("https://news.ycombinator.com/rss", False),
    ("https://reddit.com.evil.com/r/x/.rss", False),
    ("https://evil.com/feed?ref=reddit.com", False),
])
def test_is_reddit_feed_url(url, is_reddit):
    assert svc.is_reddit_feed_url(url) == is_reddit


# ---------------------------------------------------------------------------
# authorize_url
# ---------------------------------------------------------------------------

def test_authorize_url_contains_params():
    url = svc.authorize_url("my_client_id", "https://host/callback", "abc123")
    assert "my_client_id" in url
    assert "abc123" in url
    assert "permanent" in url
    assert "identity" in url or "submit" in url


# ---------------------------------------------------------------------------
# fetch_reddit_feed_entries
# ---------------------------------------------------------------------------

_SAMPLE_LISTING = {
    "data": {
        "children": [
            {
                "kind": "t3",
                "data": {
                    "title": "Cool deal on GPU",
                    "permalink": "/r/buildapcsales/comments/abc123/cool_deal/",
                    "url": "https://example.com/deal",
                    "is_self": False,
                    "created_utc": 1700000000.0,
                    "selftext_html": "",
                },
            },
            {
                "kind": "t3",
                "data": {
                    "title": "Discussion post",
                    "permalink": "/r/buildapcsales/comments/def456/discussion/",
                    "url": "https://www.reddit.com/r/buildapcsales/comments/def456/discussion/",
                    "is_self": True,
                    "created_utc": 1700001000.0,
                    "selftext_html": "<p>Self post body</p>",
                },
            },
        ]
    }
}


@patch("services.reddit.get_subreddit_new")
def test_fetch_reddit_feed_entries_subreddit(mock_get_sub_new):
    mock_get_sub_new.return_value = [c["data"] for c in _SAMPLE_LISTING["data"]["children"]]
    entries = svc.fetch_reddit_feed_entries("tok", "https://old.reddit.com/r/buildapcsales/.rss")
    assert len(entries) == 2
    # Link post → external URL as link
    assert entries[0]["link"] == "https://example.com/deal"
    # Self post → permalink as link
    assert "def456" in entries[1]["link"]
    # IDs use the permalink
    assert "abc123" in entries[0]["id"]
    assert isinstance(entries[0]["published"], datetime)
    mock_get_sub_new.assert_called_once_with("tok", "buildapcsales")


@patch("services.reddit.get_user_submitted")
def test_fetch_reddit_feed_entries_redditor(mock_get_user):
    mock_get_user.return_value = [_SAMPLE_LISTING["data"]["children"][0]["data"]]
    entries = svc.fetch_reddit_feed_entries("tok", "https://old.reddit.com/user/spez/submitted/.rss")
    assert len(entries) == 1
    mock_get_user.assert_called_once_with("tok", "spez")


def test_fetch_reddit_feed_entries_unknown_url():
    entries = svc.fetch_reddit_feed_entries("tok", "https://example.com/feed")
    assert entries == []


# ---------------------------------------------------------------------------
# _posts_from_listing
# ---------------------------------------------------------------------------

def test_posts_from_listing_filters_non_t3():
    data = {
        "data": {
            "children": [
                {"kind": "t3", "data": {"title": "post"}},
                {"kind": "t1", "data": {"title": "comment"}},   # should be ignored
                {"kind": "more", "data": {}},                    # should be ignored
            ]
        }
    }
    result = svc._posts_from_listing(data)
    assert len(result) == 1
    assert result[0]["title"] == "post"


# ---------------------------------------------------------------------------
# submit_link
# ---------------------------------------------------------------------------

def test_submit_link_strips_r_prefix():
    with patch("services.reddit.httpx.Client") as mock_client_cls:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "json": {"errors": [], "data": {"url": "https://redd.it/abc", "id": "abc"}}
        }
        mock_client_cls.return_value.__enter__.return_value.post.return_value = mock_resp
        result = svc.submit_link("tok", "r/technology", "Title", "https://example.com")
    assert result["url"] == "https://redd.it/abc"


def test_submit_link_raises_on_api_error():
    with patch("services.reddit.httpx.Client") as mock_client_cls:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "json": {"errors": [["SUBREDDIT_NOTALLOWED", "You are not allowed to post here.", "sr"]]}
        }
        mock_client_cls.return_value.__enter__.return_value.post.return_value = mock_resp
        with pytest.raises(RuntimeError, match="SUBREDDIT_NOTALLOWED"):
            svc.submit_link("tok", "technology", "Title", "https://example.com")
