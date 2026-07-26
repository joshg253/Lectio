"""The in-body chrome stripper must not eat the article it is cleaning.

`_ARTICLE_CHROME_SELECTORS` removes share bars, signup boxes and ad units that
CMSes nest *inside* the article container. But the match is by class substring,
so a site whose body container legitimately carries one of those words loses
everything: selfh.st publishes a newsletter, its container matches
`[class*="newsletter"]`, and stripping it took 99.9% of the page — Readability
view showed a lead image and nothing else where it used to show the article.

Chrome is small next to the article it decorates, so text share is the signal.
"""
from __future__ import annotations

import main

PROSE = "<p>" + ("Real article prose that carries the page. " * 40) + "</p>"


def _text_len(html: str) -> int:
    from bs4 import BeautifulSoup
    return len(BeautifulSoup(html, "html.parser").get_text(" ", strip=True))


def test_article_container_named_newsletter_survives():
    """The selfh.st shape: the body container itself matches a chrome selector."""
    html = f'<html><body><div class="newsletter-content">{PROSE}</div></body></html>'
    out = main._strip_article_chrome(html)
    assert _text_len(out) >= _text_len(html) * 0.9
    assert "Real article prose" in out


def test_small_newsletter_box_is_still_stripped():
    """The case the selectors exist for — a signup box beside real content."""
    html = (
        "<html><body>"
        f'<div class="post-body">{PROSE}</div>'
        '<div class="newsletter-signup"><p>Subscribe to our newsletter!</p></div>'
        "</body></html>"
    )
    out = main._strip_article_chrome(html)
    assert "Subscribe to our newsletter" not in out
    assert "Real article prose" in out


def test_share_bar_inside_the_article_is_still_stripped():
    html = (
        "<html><body><article>"
        '<div class="social-share"><a>Tweet</a><a>Share</a></div>'
        f"{PROSE}"
        "</article></body></html>"
    )
    out = main._strip_article_chrome(html)
    assert "Tweet" not in out
    assert "Real article prose" in out


def test_page_that_is_only_chrome_is_left_alone():
    """Nothing else to keep, so removing it would leave an empty document and
    the extractor no chance at a fallback."""
    html = '<html><body><div class="newsletter"><p>Sign up for updates.</p></div></body></html>'
    out = main._strip_article_chrome(html)
    assert "Sign up for updates" in out
