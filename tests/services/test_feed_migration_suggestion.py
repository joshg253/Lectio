"""Unit tests for feed_discovery.suggest_feed_migration.

FeedBurner now serves the origin site's own homepage HTML back at a dead feed
URL (no redirect, wrong content-type) instead of 404ing, so probing the feed
URL itself just finds a circular <link rel="alternate"> pointing at itself.
suggest_feed_migration instead reads the page's <link rel="canonical"> to find
the real origin, then runs normal discovery (probe_url) there. This is a
"suggest fix" affordance for the Failing Feeds panel -- never applied
automatically, just pre-fills the existing (already-verified) Change URL flow.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from services.feed_discovery import is_known_dead_end_host, suggest_feed_migration

FEEDBURNER = "https://feeds.feedburner.com/JohnResig"
FEEDBURNER2 = "https://feeds2.feedburner.com/webupd8"


def _mock_response(url: str, ct: str, text: str = "", status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.is_success = status < 400
    resp.status_code = status
    resp.url = url
    resp.headers = {"content-type": ct}
    resp.text = text
    resp.content = text.encode()
    return resp


class TestIsKnownDeadEndHost:
    def test_feedburner_primary_host(self):
        assert is_known_dead_end_host("https://feeds.feedburner.com/Foo")

    def test_feedburner_secondary_host(self):
        assert is_known_dead_end_host("https://feeds2.feedburner.com/Foo")

    def test_ordinary_host_is_not_known(self):
        assert not is_known_dead_end_host("https://example.com/feed.xml")

    def test_case_insensitive(self):
        assert is_known_dead_end_host("https://FEEDS.FEEDBURNER.COM/Foo")


class TestSuggestFeedMigration:
    def test_non_feedburner_host_returns_no_suggestion(self):
        result = suggest_feed_migration("https://example.com/feed.xml")
        assert result["status"] == "none"
        assert result["feeds"] == []

    def test_network_error_is_reported_as_error(self):
        with patch("services.feed_discovery._guarded_get", side_effect=Exception("down")):
            result = suggest_feed_migration(FEEDBURNER)
        assert result["status"] == "error"
        assert result["feeds"] == []

    def test_non_success_response_is_an_error(self):
        with patch("services.feed_discovery._guarded_get",
                    return_value=_mock_response(FEEDBURNER, "text/xml", status=404)):
            result = suggest_feed_migration(FEEDBURNER)
        assert result["status"] == "error"

    def test_no_canonical_link_yields_no_suggestion(self):
        html = "<html><head><title>Domain parked</title></head><body>For sale</body></html>"
        with patch("services.feed_discovery._guarded_get",
                    return_value=_mock_response(FEEDBURNER, "text/xml", html)):
            result = suggest_feed_migration(FEEDBURNER)
        assert result["status"] == "none"
        assert result["feeds"] == []
        assert "canonical" in result["message"].lower()

    def test_canonical_pointing_back_at_feedburner_is_not_a_candidate(self):
        html = '<link rel="canonical" href="https://feeds.feedburner.com/JohnResig" />'
        with patch("services.feed_discovery._guarded_get",
                    return_value=_mock_response(FEEDBURNER, "text/xml", html)):
            result = suggest_feed_migration(FEEDBURNER)
        assert result["status"] == "none"
        assert result["feeds"] == []

    def test_canonical_origin_resolved_via_probe_url(self):
        """The realistic John Resig case: canonical points off-host, and the
        origin's own page directly advertises its real feed."""
        fb_html = (
            '<link href="https://johnresig.com/style.css" rel="stylesheet" />'
            '<link rel="canonical" href="https://johnresig.com/" />'
        )
        origin_html = (
            '<link rel="alternate" type="application/rss+xml" href="/feed/" title="RSS" />'
            + "<!-- padding so the body isn't mistaken for a bot-wall challenge page -->"
            + ("x" * 512)
        )

        def fake_get(url, **_kwargs):
            if url == FEEDBURNER:
                return _mock_response(FEEDBURNER, "text/xml", fb_html)
            if url == "https://johnresig.com/":
                return _mock_response("https://johnresig.com/", "text/html", origin_html)
            raise AssertionError(f"unexpected GET {url}")

        def fake_head(url, **_kwargs):
            return _mock_response(str(url), "application/rss+xml")

        with patch("services.feed_discovery._guarded_get", side_effect=fake_get):
            with patch("services.feed_discovery._guarded_head", side_effect=fake_head):
                result = suggest_feed_migration(FEEDBURNER)
        assert result["status"] == "feed"
        assert result["feeds"] == [{"url": "https://johnresig.com/feed/", "title": "RSS"}]

    def test_origin_still_advertising_the_same_dead_feedburner_url_is_not_a_suggestion(self):
        """The real live case (johnresig.com, 2026-08-25): the origin's own
        <link rel="alternate"> still names its old FeedBurner address, which
        probe_url has no way of knowing is the very address we're escaping.
        That must not come back as "found a candidate" — it's the same URL
        the caller already knows is broken."""
        fb_html = '<link rel="canonical" href="https://johnresig.com/" />'
        origin_html = (
            f'<link rel="alternate" type="application/rss+xml" href="{FEEDBURNER}" title="RSS" />'
            + ("x" * 512)
        )

        def fake_get(url, **_kwargs):
            if url == FEEDBURNER:
                return _mock_response(FEEDBURNER, "text/xml", fb_html)
            if url == "https://johnresig.com/":
                return _mock_response("https://johnresig.com/", "text/html", origin_html)
            raise AssertionError(f"unexpected GET {url}")

        def fake_head(url, **_kwargs):
            return _mock_response(str(url), "application/rss+xml")

        with patch("services.feed_discovery._guarded_get", side_effect=fake_get):
            with patch("services.feed_discovery._guarded_head", side_effect=fake_head):
                result = suggest_feed_migration(FEEDBURNER)
        assert result["status"] == "none"
        assert result["feeds"] == []

    def test_canonical_origin_with_no_live_feed_yields_no_suggestion(self):
        fb_html = '<link rel="canonical" href="https://dead-origin.example/" />'
        origin_html = "<html><body>Nothing here</body></html>" + ("x" * 512)

        def fake_get(url, **_kwargs):
            if url == FEEDBURNER:
                return _mock_response(FEEDBURNER, "text/xml", fb_html)
            return _mock_response("https://dead-origin.example/", "text/html", origin_html)

        no_match = MagicMock()
        no_match.is_success = False

        with patch("services.feed_discovery._guarded_get", side_effect=fake_get):
            with patch("services.feed_discovery._guarded_head", return_value=no_match):
                result = suggest_feed_migration(FEEDBURNER)
        assert result["status"] == "none"
        assert result["feeds"] == []

    def test_feeds2_host_also_supported(self):
        html = "<html></html>"
        with patch("services.feed_discovery._guarded_get",
                    return_value=_mock_response(FEEDBURNER2, "text/xml", html)):
            result = suggest_feed_migration(FEEDBURNER2)
        assert result["status"] == "none"
        assert result["message"] != "No known migration for this feed's host."
