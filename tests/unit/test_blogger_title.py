"""_display_title recovers a readable title for Blogger posts that ship an empty
feed <title> (the real title lives in the body heading + URL slug), while leaving
genuinely-untitled posts on other sites alone."""
from __future__ import annotations

import types

import main


def _entry(title, feed_url, link):
    return types.SimpleNamespace(title=title, feed_url=feed_url, link=link)


def test_slug_humanized():
    assert (
        main._title_from_blogger_slug(
            "https://treecardgames.blogspot.com/2026/06/gin-rummy-strategies-essential.html"
        )
        == "Gin Rummy Strategies Essential"
    )


def test_slug_handles_no_extension():
    assert main._title_from_blogger_slug("https://x.blogspot.com/2026/06/hello-world") == "Hello World"


def test_recovers_blogger_empty_title():
    e = _entry(
        "",
        "https://treecardgames.blogspot.com/feeds/posts/default",
        "https://treecardgames.blogspot.com/2026/01/the-bliss-of-playing.html",
    )
    assert main._display_title(e) == "The Bliss Of Playing"


def test_keeps_existing_title():
    e = _entry("Real Title", "https://x.blogspot.com/feeds/posts/default", "https://x.blogspot.com/p.html")
    assert main._display_title(e) == "Real Title"


def test_non_blogger_empty_title_stays_empty():
    # Tumblr reblogs are legitimately untitled — don't fabricate a slug title.
    e = _entry("", "https://www.tumblr.com/x/rss", "https://www.tumblr.com/blog/siamo-con-il-dio")
    assert main._display_title(e) == ""
