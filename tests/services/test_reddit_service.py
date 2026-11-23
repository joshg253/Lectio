"""Tests for the Reddit OAuth/feed service."""

from __future__ import annotations

from datetime import datetime

import pytest

from services import reddit as svc

# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://old.reddit.com/r/technology/.rss", "technology"),
        ("https://www.reddit.com/r/buildapcsales/.rss", "buildapcsales"),
        ("https://old.reddit.com/r/deals/new/.rss", "deals"),
        ("https://old.reddit.com/user/spez/submitted/.rss", None),
        ("https://example.com/feed", None),
    ],
)
def test_subreddit_from_feed_url(url, expected):
    assert svc.subreddit_from_feed_url(url) == expected


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://old.reddit.com/user/spez/submitted/.rss", "spez"),
        ("https://www.reddit.com/user/automoderator/submitted/", "automoderator"),
        ("https://old.reddit.com/r/technology/.rss", None),
    ],
)
def test_redditor_from_feed_url(url, expected):
    assert svc.redditor_from_feed_url(url) == expected


@pytest.mark.parametrize(
    "url, is_reddit",
    [
        ("https://old.reddit.com/r/technology/.rss", True),
        ("https://www.reddit.com/r/buildapcsales/.rss", True),
        ("https://example.com/feed", False),
        ("https://news.ycombinator.com/rss", False),
        ("https://reddit.com.evil.com/r/x/.rss", False),
        ("https://evil.com/feed?ref=reddit.com", False),
        ("https://reddit.com./r/x/.rss", True),  # trailing-dot host
        ("https://OLD.REDDIT.COM/r/x/.rss", True),  # case-insensitive host
        ("not a url", False),  # malformed
        ("http:///r/x/.rss", False),  # empty host
    ],
)
def test_is_reddit_feed_url(url, is_reddit):
    assert svc.is_reddit_feed_url(url) == is_reddit


# ---------------------------------------------------------------------------
# post_to_entry
# ---------------------------------------------------------------------------

_SAMPLE_LISTING: dict = {
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


def test_post_to_entry_links_and_ids():
    feed = "https://www.reddit.com/r/buildapcsales/.rss"
    link_post, self_post = (svc.post_to_entry(c["data"], feed) for c in _SAMPLE_LISTING["data"]["children"])
    assert link_post["link"] == "https://example.com/deal"  # link post: the external URL
    assert "def456" in self_post["link"]  # self post: the thread
    assert link_post["id"] == "https://www.reddit.com/r/buildapcsales/comments/abc123/cool_deal/"
    assert isinstance(link_post["published"], datetime)


# ---------------------------------------------------------------------------
# _posts_from_listing
# ---------------------------------------------------------------------------


def test_posts_from_listing_filters_non_t3():
    data = {
        "data": {
            "children": [
                {"kind": "t3", "data": {"title": "post"}},
                {"kind": "t1", "data": {"title": "comment"}},  # should be ignored
                {"kind": "more", "data": {}},  # should be ignored
            ]
        }
    }
    result = svc._posts_from_listing(data)
    assert len(result) == 1
    assert result[0]["title"] == "post"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("r/Python", "https://www.reddit.com/r/Python/.rss"),
        ("/r/Python/", "https://www.reddit.com/r/Python/.rss"),
        ("u/spez", "https://www.reddit.com/user/spez/submitted/.rss"),
        ("/user/spez", "https://www.reddit.com/user/spez/submitted/.rss"),
        ("https://www.reddit.com/r/deals/", "https://www.reddit.com/r/deals/.rss"),
        ("https://old.reddit.com/r/deals/new/.rss", "https://www.reddit.com/r/deals/.rss"),
        ("https://www.reddit.com/user/spez/submitted/", "https://www.reddit.com/user/spez/submitted/.rss"),
        ("https://reddit.com.evil.com/r/x/", None),
        ("https://example.com/r/x", None),
        ("random text", None),
        ("", None),
    ],
)
def test_reddit_feed_url_from_input(text, expected):
    assert svc.reddit_feed_url_from_input(text) == expected


def test_post_to_entry_sanitizes_and_decodes_the_body():
    post = {
        "permalink": "/r/x/comments/a/t/",
        "title": "Tom &amp; Jerry",
        "is_self": True,
        "selftext_html": (
            '&lt;div class="md"&gt;&lt;p&gt;hi&lt;/p&gt;&lt;img src=x onerror=alert(1)&gt;&lt;script&gt;alert(2)&lt;/script&gt;&lt;/div&gt;'
        ),
    }
    entry = svc.post_to_entry(post, "https://www.reddit.com/r/x/.rss")
    assert entry["title"] == "Tom & Jerry"
    assert "<p>hi</p>" in entry["summary"]
    assert "onerror" not in entry["summary"] and "<script" not in entry["summary"]


def test_post_to_entry_sanitizes_unescaped_html_too():
    post = {"permalink": "/r/x/comments/a/t/", "selftext_html": "<p>ok</p><script>bad()</script>"}
    assert "<script" not in svc.post_to_entry(post, "u")["summary"]


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://www.reddit.com/r/Python/.rss", ("sub", ["Python"])),
        ("https://old.reddit.com/r/a+b+c/new/.rss", ("sub", ["a", "b", "c"])),
        ("https://www.reddit.com/user/spez/submitted/.rss", ("user", ["spez"])),
        ("https://www.reddit.com/r/foo/search.rss?q=x&restrict_sr=1", None),
        ("https://www.reddit.com/r/foo/top/.rss?t=week", None),
        ("https://www.reddit.com/r/foo/comments/abc/title/.rss", None),
        ("https://www.reddit.com/user/spez/.rss", None),
        ("https://www.reddit.com/.rss", None),
        ("https://www.reddit.com/r/popular/hot/.rss", None),
    ],
)
def test_feed_target(url, expected):
    assert svc.feed_target(url) == expected


@pytest.mark.parametrize("text", ["r/foo-bar", "https://www.reddit.com/r/foo-bar/"])
def test_hyphenated_subreddit_input_is_rejected(text):
    assert svc.reddit_feed_url_from_input(text) is None


def test_hyphenated_user_is_fine():
    assert svc.reddit_feed_url_from_input("u/some-user") == "https://www.reddit.com/user/some-user/submitted/.rss"


@pytest.mark.parametrize("text", ["reddit.com/r/Python", "www.reddit.com/r/Python/", "old.reddit.com/r/Python/new"])
def test_schemeless_reddit_urls_are_recognized(text):
    assert svc.reddit_feed_url_from_input(text) == "https://www.reddit.com/r/Python/.rss"


def test_feed_target_rejects_query_filters():
    assert svc.feed_target("https://www.reddit.com/r/foo/.rss?q=flair%3AUS&restrict_sr=on") is None
    assert svc.feed_target("https://www.reddit.com/r/foo/.rss?sort=new") == ("sub", ["foo"])
