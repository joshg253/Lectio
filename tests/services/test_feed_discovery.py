"""Unit tests for feed_discovery.discover_feed_urls."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

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
        with patch(
            "services.feed_discovery._guarded_get",
            return_value=_mock_response("https://example.com/feed.xml", "application/rss+xml"),
        ):
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

    def test_discover_drops_a_gone_link_when_no_alternative(self):
        """A 404 advertised link with nothing else on the site is reported as
        "no feed" rather than handed back. Offering it produced the worst
        outcome: the dialog says it found a feed, the add then refuses it, and
        nothing appears in the feed list (leereilly.net, 2026-07-25)."""
        with patch("services.feed_discovery._guarded_get", return_value=_mock_response("https://example.com/", "text/html", self.HTML)):
            with patch("services.feed_discovery._guarded_head", side_effect=self._head({})):
                result = discover_feed_urls("https://example.com/")
        assert result == []

    def test_discover_keeps_a_refused_link_when_no_alternative(self):
        """403 is the server refusing to answer a HEAD, not proof the feed is
        absent — reader's real GET may well get through, so it is still
        offered. This is the bot-walled case the last resort exists for."""
        def refused(url, **_kwargs):
            return _mock_response(str(url), "text/html", status=403)
        with patch("services.feed_discovery._guarded_get", return_value=_mock_response("https://example.com/", "text/html", self.HTML)):
            with patch("services.feed_discovery._guarded_head", side_effect=refused):
                result = discover_feed_urls("https://example.com/")
        assert result == ["https://example.com/rss"]

    def test_probe_url_falls_back_to_common_path(self):
        with patch("services.feed_discovery._guarded_get", return_value=_mock_response("https://example.com/", "text/html", self.HTML)):
            with patch("services.feed_discovery._guarded_head", side_effect=self._head({"/feed.xml": "application/xml"})):
                result = probe_url("https://example.com/")
        assert result["status"] == "feed"
        assert result["feeds"] == [{"url": "https://example.com/feed.xml", "title": None}]

    def test_probe_url_drops_a_gone_link_and_points_at_page_feed(self):
        with patch("services.feed_discovery._guarded_get", return_value=_mock_response("https://example.com/", "text/html", self.HTML)):
            with patch("services.feed_discovery._guarded_head", side_effect=self._head({})):
                result = probe_url("https://example.com/")
        assert result["status"] == "none"
        assert result["feeds"] == []
        assert "https://example.com/rss" in result["message"]
        assert "Page Feed" in result["message"]

    def test_probe_url_keeps_a_refused_link_when_no_alternative(self):
        def refused(url, **_kwargs):
            return _mock_response(str(url), "text/html", status=403)
        with patch("services.feed_discovery._guarded_get", return_value=_mock_response("https://example.com/", "text/html", self.HTML)):
            with patch("services.feed_discovery._guarded_head", side_effect=refused):
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


class TestRedirectingAdvertisedFeed:
    """A stale autodiscovery tag is often an ``http://`` URL. The dead-link
    probe used to stop at its 301 and keep the link, never seeing the 404
    behind it — so discovery offered a feed the add then refused
    (leereilly.net, reported 2026-07-25).
    """

    HTML = (
        '<html><head>'
        '<link rel="alternate" type="application/rss+xml" href="http://example.com/feed.xml" />'
        '</head><body>' + ("x" * 600) + '</body></html>'
    )

    @staticmethod
    def _head(status_by_url):
        def fake_head(url, **_kwargs):
            url = str(url)
            spec = status_by_url.get(url)
            if spec is None:
                return _mock_response(url, "text/html", status=404)
            if isinstance(spec, tuple):  # (status, location)
                resp = _mock_response(url, "text/html", status=spec[0])
                resp.headers = {"location": spec[1]}
                resp.is_redirect = True
                return resp
            return _mock_response(url, spec)
        return fake_head

    def test_dead_link_behind_a_redirect_loses_to_a_working_alternative(self):
        heads = {
            "http://example.com/feed.xml": (301, "https://example.com/feed.xml"),
            "https://example.com/feed.xml": None,          # 404 — the truth
            "https://example.com/rss": "application/rss+xml",  # the live feed
        }
        with patch("services.feed_discovery._guarded_get",
                   return_value=_mock_response("https://example.com/", "text/html", self.HTML)):
            with patch("services.feed_discovery._guarded_head", side_effect=self._head(heads)):
                result = discover_feed_urls("https://example.com/")
        assert result == ["https://example.com/rss"]

    def test_live_feed_behind_a_redirect_is_kept(self):
        heads = {
            "http://example.com/feed.xml": (301, "https://example.com/feed.xml"),
            "https://example.com/feed.xml": "application/rss+xml",
        }
        with patch("services.feed_discovery._guarded_get",
                   return_value=_mock_response("https://example.com/", "text/html", self.HTML)):
            with patch("services.feed_discovery._guarded_head", side_effect=self._head(heads)):
                result = discover_feed_urls("https://example.com/")
        assert result == ["http://example.com/feed.xml"]

    def test_redirect_loop_keeps_the_link(self):
        """Inconclusive, so the conservative default stands."""
        heads = {
            "http://example.com/feed.xml": (301, "https://example.com/feed.xml"),
            "https://example.com/feed.xml": (301, "http://example.com/feed.xml"),
        }
        with patch("services.feed_discovery._guarded_get",
                   return_value=_mock_response("https://example.com/", "text/html", self.HTML)):
            with patch("services.feed_discovery._guarded_head", side_effect=self._head(heads)):
                result = discover_feed_urls("https://example.com/")
        assert result == ["http://example.com/feed.xml"]


class TestMultisitePathScopedFeeds:
    """Multisite WordPress puts a whole blog under a path
    (devblogs.microsoft.com/oldnewthing/) while the domain root serves a
    firehose of every blog on it. The conventional-path probe used to try the
    root first, so subscribing to "The Old New Thing" silently handed back
    "Microsoft for Developers" — reported from the live site 2026-07-25.
    """

    HTML = "<html><head><title>The Old New Thing</title></head><body>" + ("x" * 600) + "</body></html>"

    @staticmethod
    def _head(alive_paths):
        def fake_head(url, **_kwargs):
            for path, ct in alive_paths.items():
                if url == f"https://devblogs.microsoft.com{path}":
                    return _mock_response(url, ct)
            return _mock_response(url, "text/html", status=404)
        return fake_head

    def _probe(self, page, alive):
        with patch("services.feed_discovery._guarded_get",
                   return_value=_mock_response(page, "text/html", self.HTML)):
            with patch("services.feed_discovery._guarded_head", side_effect=self._head(alive)):
                return probe_url(page)

    def test_path_scoped_feed_beats_the_root_firehose(self):
        result = self._probe(
            "https://devblogs.microsoft.com/oldnewthing/",
            {"/oldnewthing/feed": "application/rss+xml", "/feed": "application/rss+xml"},
        )
        assert result["feeds"] == [
            {"url": "https://devblogs.microsoft.com/oldnewthing/feed", "title": None}
        ]

    def test_root_feed_still_found_when_the_path_has_none(self):
        result = self._probe(
            "https://devblogs.microsoft.com/nosuchblog/",
            {"/feed": "application/rss+xml"},
        )
        assert result["feeds"] == [{"url": "https://devblogs.microsoft.com/feed", "title": None}]

    def test_root_url_is_unaffected(self):
        result = self._probe("https://devblogs.microsoft.com/", {"/feed": "application/rss+xml"})
        assert result["feeds"] == [{"url": "https://devblogs.microsoft.com/feed", "title": None}]


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


class TestArtstationFeedRewrite:
    """ArtStation walls the subdomain feed and the profile page (403 even to a
    browser), but www.artstation.com/<user>.rss serves the feed. Map both
    profile forms onto it."""

    EXPECTED = "https://www.artstation.com/sidre1.rss"

    def test_profile_www(self):
        assert rewrite_known_site_url("https://www.artstation.com/sidre1") == self.EXPECTED

    def test_profile_bare_host(self):
        assert rewrite_known_site_url("https://artstation.com/sidre1") == self.EXPECTED

    def test_subdomain(self):
        assert rewrite_known_site_url("https://sidre1.artstation.com/") == self.EXPECTED

    def test_subdomain_rss_path(self):
        # The blocked subdomain /rss form still resolves to the working www feed.
        assert rewrite_known_site_url("https://sidre1.artstation.com/rss") == self.EXPECTED

    def test_already_rss_passes_through(self):
        url = "https://www.artstation.com/sidre1.rss"
        assert rewrite_known_site_url(url) == url

    def test_reserved_site_pages_unchanged(self):
        for path in ("search", "jobs", "prints", "marketplace", "learning", "2d"):
            url = f"https://www.artstation.com/{path}"
            assert rewrite_known_site_url(url) == url

    def test_deep_paths_unchanged(self):
        # A specific artwork, not a profile — leave it for generic discovery.
        url = "https://www.artstation.com/artwork/abc123"
        assert rewrite_known_site_url(url) == url

    def test_www_subdomain_not_treated_as_user(self):
        assert rewrite_known_site_url("https://www.artstation.com/") == \
            "https://www.artstation.com/"


class TestBehanceFeedRewrite:
    """Behance per-user feeds live at www.behance.net/<user>.rss; the profile
    page is HTML. Map the profile URL onto the .rss form."""

    EXPECTED = "https://www.behance.net/polibear.rss"

    def test_profile_www(self):
        assert rewrite_known_site_url("https://www.behance.net/polibear") == self.EXPECTED

    def test_profile_bare_host(self):
        assert rewrite_known_site_url("https://behance.net/polibear") == self.EXPECTED

    def test_already_rss_passes_through(self):
        url = "https://www.behance.net/polibear.rss"
        assert rewrite_known_site_url(url) == url

    def test_feeds_user_form_untouched(self):
        url = "https://www.behance.net/feeds/user?username=polibear"
        assert rewrite_known_site_url(url) == url

    def test_reserved_pages_unchanged(self):
        for path in ("search", "galleries", "joblist", "hire", "for_you"):
            url = f"https://www.behance.net/{path}"
            assert rewrite_known_site_url(url) == url

    def test_deep_path_unchanged(self):
        url = "https://www.behance.net/gallery/12345/Project-Name"
        assert rewrite_known_site_url(url) == url


class TestFreeCodeCampFeedRewrite:
    """freeCodeCamp News (Ghost) has a feed per collection at <path>/rss/, but a
    tag/author page advertises the site-wide feed — so a tag page must be mapped
    to its own feed, not the firehose."""

    def test_tag_page_maps_to_tag_feed(self):
        assert rewrite_known_site_url("https://www.freecodecamp.org/news/tag/advanced-mathematics/") == \
            "https://www.freecodecamp.org/news/tag/advanced-mathematics/rss/"

    def test_tag_page_without_trailing_slash(self):
        assert rewrite_known_site_url("https://www.freecodecamp.org/news/tag/python") == \
            "https://www.freecodecamp.org/news/tag/python/rss/"

    def test_author_page_maps_to_author_feed(self):
        assert rewrite_known_site_url("https://www.freecodecamp.org/news/author/quincy/") == \
            "https://www.freecodecamp.org/news/author/quincy/rss/"

    def test_news_root_maps_to_site_feed(self):
        assert rewrite_known_site_url("https://www.freecodecamp.org/news/") == \
            "https://www.freecodecamp.org/news/rss/"

    def test_already_a_feed_passes_through(self):
        for url in (
            "https://www.freecodecamp.org/news/rss/",
            "https://www.freecodecamp.org/news/tag/python/rss/",
        ):
            assert rewrite_known_site_url(url) == url

    def test_article_url_falls_through(self):
        # An article has no feed — leave it for generic discovery, don't 404 on /rss/.
        url = "https://www.freecodecamp.org/news/how-to-learn-python/"
        assert rewrite_known_site_url(url) == url

    def test_non_news_path_unchanged(self):
        url = "https://www.freecodecamp.org/learn/"
        assert rewrite_known_site_url(url) == url


class TestTapasDiscovery:
    """Tapas advertises no <link rel=alternate> at all — its only alternate is
    the mobile page, and the canonical link points at the latest *episode*. So
    the series id has to come from the URL (numeric form) or the page body
    (slug form), which is what the community userscripts do by hand."""

    def test_numeric_series_url_is_a_pure_rewrite(self):
        assert rewrite_known_site_url("https://tapas.io/series/217452") == \
            "https://tapas.io/rss/series/217452"

    def test_www_and_mobile_hosts(self):
        for url in ("https://www.tapas.io/series/2007", "https://m.tapas.io/series/2007"):
            assert rewrite_known_site_url(url) == "https://tapas.io/rss/series/2007"

    def test_trailing_slash_and_case(self):
        assert rewrite_known_site_url("https://Tapas.io/series/62967/") == \
            "https://tapas.io/rss/series/62967"

    def test_slug_url_is_left_for_the_body_extractor(self):
        # No fetch here, so the rewriter must not invent an id from the slug.
        url = "https://tapas.io/series/club_cryptid"
        assert rewrite_known_site_url(url) == url

    def test_feed_url_passes_through(self):
        url = "https://tapas.io/rss/series/217452"
        assert rewrite_known_site_url(url) == url

    def test_episode_url_is_not_rewritten(self):
        url = "https://tapas.io/episode/2294111"
        assert rewrite_known_site_url(url) == url

    def test_slug_page_resolves_through_the_body(self):
        # Shape of the real page: a recommendation card's id appears in the
        # markup before the page's own `seriesId:` script variable.
        html = (
            '<html><head><link rel="alternate" media="only screen and (max-width: 640px)"'
            ' href="https://m.tapas.io/episode/2294111">'
            '<link rel="canonical" href="https://tapas.io/episode/2294111"/></head>'
            '<body><a class="card" data-series-id="96914">something else</a>'
            '<script>var seriesId: 217452;</script></body></html>'
        )
        with patch("services.feed_discovery._guarded_get",
                   return_value=_mock_response("https://tapas.io/series/club_cryptid", "text/html", html)):
            with patch("services.feed_discovery._guarded_head", side_effect=_head_alive):
                result = discover_feed_urls("https://tapas.io/series/club_cryptid")
        assert result == ["https://tapas.io/rss/series/217452"]

    def test_episode_page_falls_back_to_the_first_data_series_id(self):
        # Episode pages carry no `seriesId:`; the first data-series-id is the
        # episode's own series, the rest are recommendation cards.
        html = (
            '<div data-series-id="155459"><h1>FANGS</h1></div>'
            '<a data-series-id="58981">recommended</a>'
        )
        with patch("services.feed_discovery._guarded_get",
                   return_value=_mock_response("https://tapas.io/episode/1234567", "text/html", html)):
            with patch("services.feed_discovery._guarded_head", side_effect=_head_alive):
                result = discover_feed_urls("https://tapas.io/episode/1234567")
        assert result == ["https://tapas.io/rss/series/155459"]

    def test_a_tapas_page_with_no_id_falls_through(self):
        html = "<html><body>nothing useful here</body></html>"
        no_match = MagicMock()
        no_match.is_success = False
        with patch("services.feed_discovery._guarded_get",
                   return_value=_mock_response("https://tapas.io/series/gone", "text/html", html)):
            with patch("services.feed_discovery._guarded_head", return_value=no_match):
                result = discover_feed_urls("https://tapas.io/series/gone")
        assert result == []

    def test_the_body_extractor_does_not_fire_on_other_hosts(self):
        # data-series-id is a generic-looking attribute; it must not be read on
        # a site that merely happens to use the same name.
        html = '<div data-series-id="999">not tapas</div>'
        no_match = MagicMock()
        no_match.is_success = False
        with patch("services.feed_discovery._guarded_get",
                   return_value=_mock_response("https://example.com/series/x", "text/html", html)):
            with patch("services.feed_discovery._guarded_head", return_value=no_match):
                result = discover_feed_urls("https://example.com/series/x")
        assert result == []

    def test_an_advertised_feed_still_wins_over_the_body(self):
        html = (
            '<link rel="alternate" type="application/rss+xml" href="/real/feed.xml" />'
            '<div data-series-id="217452"></div>'
        )
        with patch("services.feed_discovery._guarded_get",
                   return_value=_mock_response("https://tapas.io/series/club_cryptid", "text/html", html)):
            with patch("services.feed_discovery._guarded_head", side_effect=_head_alive):
                result = discover_feed_urls("https://tapas.io/series/club_cryptid")
        assert result == ["https://tapas.io/real/feed.xml"]

    def test_the_dialog_preview_and_the_add_path_agree(self):
        # probe_url previews what Add Feed shows; discover_feed_urls_ex is what
        # the button actually subscribes. A body extractor wired into only one
        # of them would promise one feed and subscribe another.
        # Padded past 512 bytes: a shorter body trips probe_url's
        # empty-response / bot-protection guard before discovery runs.
        html = '<div data-series-id="217452"></div>' + "<p>episode</p>" * 60
        url = "https://tapas.io/series/club_cryptid"
        with patch("services.feed_discovery._guarded_get",
                   return_value=_mock_response(url, "text/html", html)):
            with patch("services.feed_discovery._guarded_head", side_effect=_head_alive):
                preview = probe_url(url)
                added = discover_feed_urls(url)
        assert [f["url"] for f in preview["feeds"]] == added == ["https://tapas.io/rss/series/217452"]


class TestTinyviewRewrite:
    """Tinyview comics publish at /<comic>/feed.rss.

    The comic page returns 200 with no <link rel="alternate"> at all — the site
    renders client-side — so generic discovery correctly finds nothing and Add
    Feed failed on a URL whose feed is entirely predictable.
    """

    EXPECTED = "https://tinyview.com/they-can-talk/feed.rss"

    def test_comic_page(self):
        assert rewrite_known_site_url("https://tinyview.com/they-can-talk") == self.EXPECTED

    def test_trailing_slash(self):
        assert rewrite_known_site_url("https://tinyview.com/they-can-talk/") == self.EXPECTED

    def test_www_host(self):
        assert rewrite_known_site_url("https://www.tinyview.com/they-can-talk") == self.EXPECTED

    def test_episode_url_resolves_to_the_comic_feed(self):
        assert rewrite_known_site_url(
            "https://tinyview.com/they-can-talk/2026/08/13/time") == self.EXPECTED

    def test_feed_url_is_left_alone(self):
        assert rewrite_known_site_url(self.EXPECTED) == self.EXPECTED

    def test_site_pages_are_not_comics(self):
        for path in ("about", "subscribe", "search"):
            url = f"https://tinyview.com/{path}"
            assert rewrite_known_site_url(url) == url, path

    def test_bare_host_and_other_sites_untouched(self):
        for url in ("https://tinyview.com", "https://example.com/they-can-talk"):
            assert rewrite_known_site_url(url) == url, url
