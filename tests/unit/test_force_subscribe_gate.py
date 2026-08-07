"""Which failed probes may be overridden with "Subscribe Anyway".

The distinction is the whole point of the gate: a site's refusal is
overridable (it may let up later), a page with no feed is not (subscribing
to it makes a husk feed), and our OWN SSRF refusal is never overridable.
"""
from __future__ import annotations

from services.feed_discovery import refusal_is_forceable


def test_http_403_is_forceable():
    """The treblezine case: the feed URL may be real, just walled."""
    assert refusal_is_forceable(
        {"status": "error", "feeds": [], "message": "HTTP 403 — server denied the request."})


def test_timeout_is_forceable():
    assert refusal_is_forceable({"status": "error", "message": "Connection timed out."})


def test_bot_protection_is_forceable():
    assert refusal_is_forceable(
        {"status": "blocked", "message": "The site returned an empty response…"})


def test_page_without_a_feed_is_not_forceable():
    """We fetched it fine; it is an article. Forcing makes a husk feed."""
    assert not refusal_is_forceable({"status": "none", "message": "No RSS/Atom feed found."})


def test_ssrf_refusal_is_never_forceable():
    """Shares status 'blocked' with bot protection, and must not share its
    treatment — the force path skips discovery, so this probe is the only
    thing standing between a private address and a subscription."""
    assert not refusal_is_forceable(
        {"status": "blocked", "reason": "unsafe",
         "message": "That address is not allowed (private/loopback target)."})


def test_a_successful_probe_is_not_a_refusal():
    assert not refusal_is_forceable({"status": "feed", "feeds": [{"url": "x"}]})
    assert not refusal_is_forceable({})
