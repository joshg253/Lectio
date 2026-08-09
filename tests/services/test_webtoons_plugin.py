"""Webtoons episode pages advertise the *series* thumbnail as og:image — the
same `.../thumbnail.jpg` on every episode of a series. The plugin used to
conclude the feed was equally useless and suppressed lead images entirely, so
episodes rendered with the series thumbnail (from a pre-plugin cache row) or
with nothing at all.

The feed does carry the episode's own panel, on the same CDN, distinct per
episode. These pin that: page never, feed always.
"""
from __future__ import annotations

from services.lead_image_plugins import WebtoonsPlugin

ENTRY = (
    "https://www.webtoons.com/en/canvas/sarahs-scribbles/i-should-be-vs-i-am/"
    "viewer?title_no=677113&episode_no=1"
)
# The episode's own panel, straight out of the feed's <description>.
PANEL = (
    "https://swebtoon-phinf.pstatic.net/20210813_280/1628808043747imSaI_JPEG/"
    "40f5276f-e150-4921-a052-9d1da6a6653a.jpg"
)
# The og:image — identical for every episode of the series.
SERIES_THUMB = (
    "https://swebtoon-phinf.pstatic.net/20210813_258/1628807985198lpcHt_JPEG/thumbnail.jpg"
)
SUMMARY = f'<a href="{ENTRY}">\n<img src="{PANEL}"/>\n</a><br/>'


def test_the_episode_panel_comes_from_the_feed_body():
    assert WebtoonsPlugin().fallback_lead_image_url(
        entry_link=ENTRY, content_html=None, summary=SUMMARY
    ) == PANEL


def test_content_html_is_preferred_over_summary():
    other = PANEL.replace("40f5276f", "aaaaaaaa")
    assert WebtoonsPlugin().fallback_lead_image_url(
        entry_link=ENTRY,
        content_html=f'<img src="{other}"/>',
        summary=SUMMARY,
    ) == other


def test_a_feed_body_holding_only_the_series_thumbnail_yields_nothing():
    """Better no image than the same thumbnail on every episode."""
    assert WebtoonsPlugin().fallback_lead_image_url(
        entry_link=ENTRY, content_html=None, summary=f'<img src="{SERIES_THUMB}"/>'
    ) is None


def test_other_hosts_are_untouched():
    p = WebtoonsPlugin()
    assert p.fallback_lead_image_url(
        entry_link="https://example.com/post", content_html=None, summary=SUMMARY
    ) is None
    assert p.should_bypass_cached_url(
        entry_link="https://example.com/post", cached_url=SERIES_THUMB
    ) is False
    assert p.should_skip_source_lookup(entry_link="https://example.com/post") is False


def test_a_cached_series_thumbnail_is_bypassed_but_a_panel_is_kept():
    """Rows cached before the plugin read the feed body are thumbnails and must
    be re-resolved; re-resolving a good panel on every render is pure churn."""
    p = WebtoonsPlugin()
    assert p.should_bypass_cached_url(entry_link=ENTRY, cached_url=SERIES_THUMB) is True
    assert p.should_bypass_cached_url(entry_link=ENTRY, cached_url=PANEL) is False


def test_the_episode_page_is_never_scraped():
    # og:image there is the series thumbnail, so scraping can only make it worse.
    assert WebtoonsPlugin().should_skip_source_lookup(entry_link=ENTRY) is True


def test_thumbnail_detection_is_basename_only():
    p = WebtoonsPlugin()
    # A panel whose *directory* contains the word must not be mistaken for one.
    in_a_path = "https://swebtoon-phinf.pstatic.net/thumbnail/20210813_280/panel.jpg"
    assert p.should_bypass_cached_url(entry_link=ENTRY, cached_url=in_a_path) is False
    assert p.should_bypass_cached_url(
        entry_link=ENTRY, cached_url="https://swebtoon-phinf.pstatic.net/x/thumbnail.png"
    ) is True


class TestTapasPlugin:
    """Tapas is the same disease on a different host: the episode page's
    og:image is a social card (.png), the feed body carries the panel (.jpg).
    Unlike Webtoons the card is distinct per episode, so nothing about it reads
    as a thumbnail — what separates them is where they came from."""

    ENTRY = "https://tapas.io/episode/2721164"
    PANEL = "https://us-a.tapas.io/sa/18/cb6052ae-b35a-4b0c-b4a9-ce564e8ac612.jpg"
    CARD = "https://us-a.tapas.io/sa/34/00700065-647c-465c-9f98-d98aba76d9d6.png"
    BODY = f'<p>null</p><img src="{PANEL}"/>'

    def _p(self):
        from services.lead_image_plugins import TapasPlugin
        return TapasPlugin()

    def test_the_panel_comes_from_the_feed_body(self):
        assert self._p().fallback_lead_image_url(
            entry_link=self.ENTRY, content_html=self.BODY, summary=None
        ) == self.PANEL

    def test_the_episode_page_is_never_scraped(self):
        assert self._p().should_skip_source_lookup(entry_link=self.ENTRY) is True

    def test_a_cached_card_is_bypassed_but_a_panel_is_kept(self):
        p = self._p()
        assert p.should_bypass_cached_url(entry_link=self.ENTRY, cached_url=self.CARD) is True
        assert p.should_bypass_cached_url(entry_link=self.ENTRY, cached_url=self.PANEL) is False

    def test_series_and_www_hosts_are_covered_but_lookalikes_are_not(self):
        p = self._p()
        for link in ("https://tapas.io/series/217452", "https://www.tapas.io/episode/1"):
            assert p.should_skip_source_lookup(entry_link=link) is True
        # Suffix match must be on a label boundary, not a substring.
        assert p.should_skip_source_lookup(entry_link="https://nottapas.io/episode/1") is False
        assert p.fallback_lead_image_url(
            entry_link="https://example.com/x", content_html=self.BODY, summary=None
        ) is None
