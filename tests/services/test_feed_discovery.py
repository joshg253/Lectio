"""Unit tests for feed_discovery.discover_feed_urls."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from services.feed_discovery import _ct_is_feed, _parse_attrs, discover_feed_urls, probe_url, rewrite_known_site_url


class TestCtIsFeed:
    def test_rss_xml(self):
        assert _ct_is_feed("application/rss+xml; charset=utf-8")

    def test_atom_xml(self):
        assert _ct_is_feed("application/atom+xml")

    def test_text_xml(self):
        assert _ct_is_feed("text/xml")

    def test_application_xml(self):
        assert _ct_is_feed("application/xml")

    def test_feed_json(self):
        assert _ct_is_feed("application/feed+json")

    def test_html_not_feed(self):
        assert not _ct_is_feed("text/html; charset=utf-8")

    def test_json_not_feed(self):
        assert not _ct_is_feed("application/json")

    def test_empty_not_feed(self):
        assert not _ct_is_feed("")


class TestParseAttrs:
    def test_double_quoted(self):
        assert _parse_attrs('rel="alternate" href="/feed"') == {"rel": "alternate", "href": "/feed"}

    def test_single_quoted(self):
        assert _parse_attrs("type='application/rss+xml'") == {"type": "application/rss+xml"}

    def test_mixed_order(self):
        attrs = _parse_attrs('href="/feed.xml" rel="alternate" type="application/rss+xml"')
        assert attrs["href"] == "/feed.xml"
        assert attrs["rel"] == "alternate"
        assert attrs["type"] == "application/rss+xml"


def _mock_response(url: str, ct: str, text: str = "", status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.is_success = status < 400
    resp.status_code = status
    resp.url = url
    resp.headers = {"content-type": ct}
    resp.text = text
    resp.content = text.encode()
    return resp


def _head_alive(url, **_kwargs):
    """HEAD stub for tests where advertised links should validate as live."""
    return _mock_response(str(url), "application/rss+xml")


class TestDiscoverFeedUrls:
    def test_url_already_a_feed(self):
        with patch("services.feed_discovery._guarded_get", return_value=_mock_response("https://example.com/feed.xml", "application/rss+xml")):
            result = discover_feed_urls("https://example.com/feed.xml")
        assert result == ["https://example.com/feed.xml"]

    def test_html_page_with_link_tag(self):
        html = (
            '<html><head>'
            '<link rel="alternate" type="application/rss+xml" href="/feed.xml" title="RSS" />'
            '</head></html>'
        )
        with patch("services.feed_discovery._guarded_get", return_value=_mock_response("https://example.com/", "text/html", html)):
            with patch("services.feed_discovery._guarded_head", side_effect=_head_alive):
                result = discover_feed_urls("https://example.com/")
        assert result == ["https://example.com/feed.xml"]

    def test_html_page_with_atom_link(self):
        html = (
            '<link type="application/atom+xml" rel="alternate" href="https://feeds.example.com/atom" />'
        )
        with patch("services.feed_discovery._guarded_get", return_value=_mock_response("https://example.com/", "text/html", html)):
            with patch("services.feed_discovery._guarded_head", side_effect=_head_alive):
                result = discover_feed_urls("https://example.com/")
        assert result == ["https://feeds.example.com/atom"]

    def test_html_page_relative_href_resolved(self):
        html = '<link rel="alternate" type="application/rss+xml" href="../rss.xml" />'
        with patch("services.feed_discovery._guarded_get", return_value=_mock_response("https://example.com/blog/", "text/html", html)):
            with patch("services.feed_discovery._guarded_head", side_effect=_head_alive):
                result = discover_feed_urls("https://example.com/blog/")
        assert result == ["https://example.com/rss.xml"]

    def test_html_no_link_tags_probes_common_paths(self):
        html = "<html><body>No feed links here</body></html>"
        head_resp = _mock_response("https://example.com/feed", "application/rss+xml")
        head_resp.url = "https://example.com/feed"

        def fake_head(url, **_kwargs):
            if url == "https://example.com/feed":
                return head_resp
            not_found = MagicMock()
            not_found.is_success = False
            return not_found

        with patch("services.feed_discovery._guarded_get", return_value=_mock_response("https://example.com/", "text/html", html)):
            with patch("services.feed_discovery._guarded_head", side_effect=fake_head):
                result = discover_feed_urls("https://example.com/")
        assert result == ["https://example.com/feed"]

    def test_network_error_returns_empty(self):
        with patch("services.feed_discovery._guarded_get", side_effect=Exception("network down")):
            result = discover_feed_urls("https://example.com/")
        assert result == []

    def test_non_success_response_returns_empty(self):
        with patch("services.feed_discovery._guarded_get", return_value=_mock_response("https://example.com/", "text/html", status=404)):
            result = discover_feed_urls("https://example.com/")
        assert result == []

    def test_no_feeds_found_returns_empty(self):
        html = "<html><body>No feeds here</body></html>"
        no_match = MagicMock()
        no_match.is_success = False

        with patch("services.feed_discovery._guarded_get", return_value=_mock_response("https://example.com/", "text/html", html)):
            with patch("services.feed_discovery._guarded_head", return_value=no_match):
                result = discover_feed_urls("https://example.com/")
        assert result == []

    def test_deduplicates_candidates(self):
        html = (
            '<link rel="alternate" type="application/rss+xml" href="/feed.xml" />'
            '<link rel="alternate" type="application/atom+xml" href="/feed.xml" />'
        )
        with patch("services.feed_discovery._guarded_get", return_value=_mock_response("https://example.com/", "text/html", html)):
            with patch("services.feed_discovery._guarded_head", side_effect=_head_alive):
                result = discover_feed_urls("https://example.com/")
        assert result == ["https://example.com/feed.xml"]

    def test_declaration_order_preserved(self):
        """Feeds are returned in the order they appear in the HTML, regardless of format."""
        html = (
            '<link rel="alternate" type="application/atom+xml" href="/atom.xml" />'
            '<link rel="alternate" type="application/rss+xml" href="/rss.xml" />'
        )
        with patch("services.feed_discovery._guarded_get", return_value=_mock_response("https://example.com/", "text/html", html)):
            with patch("services.feed_discovery._guarded_head", side_effect=_head_alive):
                result = discover_feed_urls("https://example.com/")
        assert result == ["https://example.com/atom.xml", "https://example.com/rss.xml"]

    def test_subdir_path_relative_probing(self):
        """Falls back to page-path-relative probing for subdirectory-hosted blogs."""
        html = "<html><body>No feed links</body></html>"
        head_resp = _mock_response("https://example.com/blog/feed/", "application/rss+xml")
        head_resp.url = "https://example.com/blog/feed/"

        def fake_head(url, **_kwargs):
            if url == "https://example.com/blog/feed/":
                return head_resp
            not_found = MagicMock()
            not_found.is_success = False
            return not_found

        with patch("services.feed_discovery._guarded_get", return_value=_mock_response("https://example.com/blog", "text/html", html)):
            with patch("services.feed_discovery._guarded_head", side_effect=fake_head):
                result = discover_feed_urls("https://example.com/blog")
        assert result == ["https://example.com/blog/feed/"]

    def test_wordpress_query_param_probing(self):
        """Falls back to ?feed=rss2 query-param probing when path probing finds nothing."""
        html = "<html><body>No feed links</body></html>"
        feed_url = "https://example.com/blog/?feed=rss2"
        head_resp = _mock_response(feed_url, "application/rss+xml")
        head_resp.url = feed_url

        def fake_head(url, **_kwargs):
            if url == feed_url:
                return head_resp
            not_found = MagicMock()
            not_found.is_success = False
            return not_found

        with patch("services.feed_discovery._guarded_get", return_value=_mock_response("https://example.com/blog", "text/html", html)):
            with patch("services.feed_discovery._guarded_head", side_effect=fake_head):
                result = discover_feed_urls("https://example.com/blog")
        assert result == [feed_url]


class TestDeadAdvertisedFallback:
    """A stale <link rel="alternate"> (feed moved, tag left behind — the
    dropmark.com case: /rss is dead, the feed lives at /feed.xml) must not
    beat a working conventional path."""

    # Padded past probe_url's small-HTML bot-challenge heuristic (512 bytes).
    HTML = ('<html><head>'
            '<link rel="alternate" type="application/rss+xml" href="/rss" title="Blog (RSS)" />'
            '</head><body>' + '<p>real page content</p>' * 30 + '</body></html>')

    @staticmethod
    def _head(alive_paths):
        def fake_head(url, **_kwargs):
            for path, ct in alive_paths.items():
                if url == f"https://example.com{path}":
                    return _mock_response(url, ct)
            return _mock_response(url, "text/html", status=404)
        return fake_head

    def test_discover_falls_back_to_common_path(self):
        with patch("services.feed_discovery._guarded_get", return_value=_mock_response("https://example.com/", "text/html", self.HTML)):
            with patch("services.feed_discovery._guarded_head", side_effect=self._head({"/feed.xml": "application/xml"})):
                result = discover_feed_urls("https://example.com/")
        assert result == ["https://example.com/feed.xml"]

    def test_discover_keeps_dead_link_when_no_alternative(self):
        with patch("services.feed_discovery._guarded_get", return_value=_mock_response("https://example.com/", "text/html", self.HTML)):
            with patch("services.feed_discovery._guarded_head", side_effect=self._head({})):
                result = discover_feed_urls("https://example.com/")
        assert result == ["https://example.com/rss"]  # last resort — unchanged behavior

    def test_probe_url_falls_back_to_common_path(self):
        with patch("services.feed_discovery._guarded_get", return_value=_mock_response("https://example.com/", "text/html", self.HTML)):
            with patch("services.feed_discovery._guarded_head", side_effect=self._head({"/feed.xml": "application/xml"})):
                result = probe_url("https://example.com/")
        assert result["status"] == "feed"
        assert result["feeds"] == [{"url": "https://example.com/feed.xml", "title": None}]

    def test_probe_url_keeps_dead_link_when_no_alternative(self):
        with patch("services.feed_discovery._guarded_get", return_value=_mock_response("https://example.com/", "text/html", self.HTML)):
            with patch("services.feed_discovery._guarded_head", side_effect=self._head({})):
                result = probe_url("https://example.com/")
        assert result["status"] == "feed"
        assert result["feeds"][0]["url"] == "https://example.com/rss"

    def test_probe_url_dead_direct_paste_falls_back(self):
        """Pasting the dead advertised URL itself (dropmark.com/rss) probes the
        origin's conventional paths instead of stopping at the HTTP error."""
        dead = _mock_response("https://example.com/rss", "text/html", "<html>404-ish</html>", status=403)
        with patch("services.feed_discovery._guarded_get", return_value=dead):
            with patch("services.feed_discovery._guarded_head", side_effect=self._head({"/feed.xml": "application/xml"})):
                result = probe_url("https://example.com/rss")
        assert result["status"] == "feed"
        assert result["feeds"] == [{"url": "https://example.com/feed.xml", "title": None}]

    def test_probe_url_dead_direct_paste_no_alternative_errors(self):
        dead = _mock_response("https://example.com/rss", "text/html", "<html>404-ish</html>", status=403)
        with patch("services.feed_discovery._guarded_get", return_value=dead):
            with patch("services.feed_discovery._guarded_head", side_effect=self._head({})):
                result = probe_url("https://example.com/rss")
        assert result["status"] == "error"
        assert "HTTP 403" in result["message"]

    def test_head_hostile_405_keeps_advertised_link(self):
        def fake_head(url, **_kwargs):
            return _mock_response(url, "text/html", status=405)
        with patch("services.feed_discovery._guarded_get", return_value=_mock_response("https://example.com/", "text/html", self.HTML)):
            with patch("services.feed_discovery._guarded_head", side_effect=fake_head):
                result = discover_feed_urls("https://example.com/")
        assert result == ["https://example.com/rss"]

    def test_head_error_keeps_advertised_link(self):
        def fake_head(url, **_kwargs):
            raise Exception("connection reset")
        with patch("services.feed_discovery._guarded_get", return_value=_mock_response("https://example.com/", "text/html", self.HTML)):
            with patch("services.feed_discovery._guarded_head", side_effect=fake_head):
                result = discover_feed_urls("https://example.com/")
        assert result == ["https://example.com/rss"]


class TestPinboardRewrite:
    """pinboard.in pages have no <link rel=alternate>; page URLs map directly
    to feeds.pinboard.in feed URLs (same u:/t:/from: segment grammar)."""

    def test_popular(self):
        assert rewrite_known_site_url("https://pinboard.in/popular/") == \
            "https://feeds.pinboard.in/rss/popular/"

    def test_recent(self):
        assert rewrite_known_site_url("https://pinboard.in/recent/") == \
            "https://feeds.pinboard.in/rss/recent/"

    def test_user(self):
        assert rewrite_known_site_url("https://pinboard.in/u:jsmith/") == \
            "https://feeds.pinboard.in/rss/u:jsmith/"

    def test_user_with_tags(self):
        assert rewrite_known_site_url("https://pinboard.in/u:jsmith/t:python/t:web/") == \
            "https://feeds.pinboard.in/rss/u:jsmith/t:python/t:web/"

    def test_tag_only(self):
        assert rewrite_known_site_url("https://pinboard.in/t:linux/") == \
            "https://feeds.pinboard.in/rss/t:linux/"

    def test_user_from_source(self):
        assert rewrite_known_site_url("https://pinboard.in/u:jsmith/from:twitter/") == \
            "https://feeds.pinboard.in/rss/u:jsmith/from:twitter/"

    def test_secret_private(self):
        assert rewrite_known_site_url("https://pinboard.in/secret:abc123/u:jsmith/private/") == \
            "https://feeds.pinboard.in/rss/secret:abc123/u:jsmith/private/"

    def test_www_host(self):
        assert rewrite_known_site_url("https://www.pinboard.in/popular/") == \
            "https://feeds.pinboard.in/rss/popular/"

    def test_explicit_port_and_case(self):
        assert rewrite_known_site_url("https://Pinboard.in:443/popular/") == \
            "https://feeds.pinboard.in/rss/popular/"

    def test_missing_trailing_slash(self):
        assert rewrite_known_site_url("https://pinboard.in/popular") == \
            "https://feeds.pinboard.in/rss/popular/"

    def test_non_feed_page_unchanged(self):
        for url in (
            "https://pinboard.in/",
            "https://pinboard.in/search/?query=x",
            "https://pinboard.in/settings/",
            "https://pinboard.in/howto/",
        ):
            assert rewrite_known_site_url(url) == url

    def test_other_host_unchanged(self):
        assert rewrite_known_site_url("https://example.com/popular/") == \
            "https://example.com/popular/"

    def test_feeds_host_untouched(self):
        # Pasting the feed URL itself must pass through unchanged.
        url = "https://feeds.pinboard.in/rss/popular/"
        assert rewrite_known_site_url(url) == url
