"""Telling a blocked feed apart from a broken one.

The distinction is not cosmetic: a block is worth an email to the site, a broken
feed wants a Change URL or an unsubscribe, and Lectio was filing the first as the
second. Poorly Drawn Lines returned a SiteGround captcha as **HTTP 202** for
months and was recorded as "could not be parsed as a valid RSS/Atom document",
so it never appeared in any count of blocked feeds.
"""
from __future__ import annotations

import pytest

from services.bot_challenge import FeedBlockedError, detect_challenge

SITEGROUND = (
    b'<html><head><link rel="icon" href="data:;"><meta http-equiv="refresh" '
    b'content="0;/.well-known/sgcaptcha/?r=%2Ffeed&y=ipr:203.0.113.9:1786511699"></meta>'
    b'</head></html>'
)
CLOUDFLARE = (
    b'<!DOCTYPE html><html><head><title>Just a moment...</title>'
    b'<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1"></script>'
    b'</head><body></body></html>'
)
REAL_FEED = (
    b'<?xml version="1.0"?><rss version="2.0"><channel><title>A feed</title>'
    b'<item><title>On Cloudflare challenge pages</title></item></channel></rss>'
)
PLAIN_HOMEPAGE = (
    b'<!DOCTYPE html><html><head><title>My Blog</title></head>'
    b'<body><h1>Welcome</h1><p>Posts below.</p></body></html>'
)


def test_siteground_captcha_is_detected():
    assert detect_challenge("text/html", SITEGROUND) == "SiteGround captcha"


def test_cloudflare_interstitial_is_detected():
    assert detect_challenge("text/html", CLOUDFLARE) == "Cloudflare challenge"


def test_status_is_irrelevant_because_vendors_disagree():
    """The whole reason this exists: SiteGround serves its captcha as 202, so
    anything keyed on 403 misses it. detect_challenge never sees a status."""
    assert detect_challenge("text/html; charset=UTF-8", SITEGROUND)


def test_a_real_feed_mentioning_cloudflare_is_not_a_challenge():
    """An XML body containing a marker word is a feed talking about Cloudflare."""
    assert detect_challenge("application/rss+xml", REAL_FEED) is None


def test_a_sites_ordinary_homepage_is_not_a_challenge():
    """A dead feed URL that now serves the homepage is a *moved/dropped* feed.
    Calling it 'blocked' would send someone chasing a block that isn't there."""
    assert detect_challenge("text/html", PLAIN_HOMEPAGE) is None


@pytest.mark.parametrize("body", [b"", None])
def test_empty_bodies_are_not_challenges(body):
    assert detect_challenge("text/html", body) is None


def test_non_html_content_type_is_skipped():
    assert detect_challenge("application/octet-stream", SITEGROUND) is None


def test_a_missing_content_type_still_sniffs():
    """Challenge pages are not always served with a Content-Type."""
    assert detect_challenge(None, SITEGROUND) == "SiteGround captcha"


def test_undecodable_bytes_do_not_raise():
    assert detect_challenge("text/html", b"\xff\xfe\x00\x01" * 50) is None


def test_the_error_message_is_greppable_and_names_the_vendor():
    """failing-feed triage buckets on this prefix, so it is part of the contract."""
    err = FeedBlockedError("SiteGround captcha", "https://example.com/feed")
    assert str(err).startswith("bot challenge: ")
    assert "SiteGround captcha" in str(err)
