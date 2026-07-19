"""FeedRefreshService.categorize_failure — buckets stored last_error text into a
fail category for the Failing Feeds filter."""
from __future__ import annotations

import pytest

from services.feed_refresh import FeedRefreshService

cat = FeedRefreshService.categorize_failure


@pytest.mark.parametrize("err, expected", [
    ("The feed server denied access (HTTP 403 Forbidden). The server may be blocking...", "blocked"),
    ("The feed server is rate-limiting requests (HTTP 429).", "rate_limited"),
    ("The feed requires authentication (HTTP 401 Unauthorized).", "auth"),
    ("The feed has been permanently removed (HTTP 410 Gone). Feed updates have been disabled.", "gone"),
    ("The feed URL returned not found (HTTP 404).", "not_found"),
    ("Could not resolve the feed host name (DNS lookup failed).", "dns"),
    ("TLS/SSL handshake failed while connecting to the feed URL.", "tls"),
    ("Connection timed out while contacting this feed server.", "timeout"),
    ("The feed response could not be parsed as a valid RSS/Atom document.", "not_feed"),
    ("The feed URL returned an HTML page instead of RSS/Atom.", "not_feed"),
    ("unknown feed type: 'https://www.codeproject.com/WebServices/ArticleRSS.aspx'", "not_feed"),
    ("some entirely novel failure", "other"),
    ("", "other"),
    (None, "other"),
])
def test_categorize(err, expected):
    assert cat(err) == expected


def test_410_beats_404_ordering():
    # "Gone" must not be misread as a generic not-found.
    assert cat("permanently removed (HTTP 410 Gone)") == "gone"


def test_labels_cover_every_category():
    labels = dict(FeedRefreshService.FAILURE_CATEGORIES)
    for key, _label in FeedRefreshService.FAILURE_CATEGORIES:
        assert labels[key]
