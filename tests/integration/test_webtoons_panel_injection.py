"""A Webtoons episode is a vertical strip cut into slices — 50 on a Backchannel
chapter, 8 on MercWorks, 5 on False Knees — and the RSS feed carries exactly one
image. Good thumbnail, poor article.

The slices are served from `webtoon-phinf`, which 403s anything without a
webtoons.com Referer. The sibling `swebtoon-phinf` host serves the same paths
with no Referer at all and is what the feed itself uses, so the URLs are
rewritten to it rather than forging a header we do not have.
"""
from __future__ import annotations

import re
from unittest.mock import patch

import main

LINK = "https://www.webtoons.com/en/canvas/false-knees/nervous/viewer?title_no=79544&episode_no=385"
FEED_IMG = "https://swebtoon-phinf.pstatic.net/20260512_183/1778519637863srilh_JPEG/77a537b3.jpg"
FEED_BODY = f'<a href="{LINK}"><img src="{FEED_IMG}"/></a><br/>'
SLICES = [
    "https://webtoon-phinf.pstatic.net/20260512_183/aaa_JPEG/one.jpg?type=q90",
    "https://webtoon-phinf.pstatic.net/20260512_21/bbb_JPEG/two.jpg?type=optimize",
    "https://webtoon-phinf.pstatic.net/20260512_99/ccc_JPEG/three.jpg",
]
# The page also embeds a recommendation strip of *other* series — a looser scan
# swept 62 URLs into an episode that has 50.
RECOMMENDED = '<img class="thmb" data-url="https://webtoon-phinf.pstatic.net/x/other.jpg?type=f160_151">'
PAGE = ("<div id='_imageList'>"
        + "".join(f'<img class="_images" data-url="{u}" src="/spinner.gif">' for u in SLICES)
        + "</div>" + RECOMMENDED)


class _Entry:
    def __init__(self, link=LINK):
        self.link = link
        self.feed_url = "https://www.webtoons.com/en/challenge/false-knees/rss?title_no=79544"
        self.id = LINK


def _inject(body=FEED_BODY, page=PAGE, entry=None):
    with patch.object(main.lead_image_service, "fetch_source_html_now",
                      return_value=(LINK, page) if page is not None else None):
        return main._inject_webtoons_episode_panels(
            body, entry or _Entry(), "https://www.webtoons.com/feed", FEED_IMG
        )


def _srcs(html_out):
    return re.findall(r'src="([^"]+)"', html_out)


def test_every_slice_lands_in_order():
    out, lead = _inject()
    assert len(_srcs(out)) == 3
    assert lead is None


def test_slices_are_rewritten_to_the_ungated_host():
    """webtoon-phinf 403s without a webtoons.com Referer, including from a
    browser loading the image off our page. swebtoon-phinf does not."""
    out, _ = _inject()
    assert all(u.startswith("https://swebtoon-phinf.pstatic.net/") for u in _srcs(out))
    assert "//webtoon-phinf." not in out


def test_the_resize_query_is_dropped():
    out, _ = _inject()
    assert all("?" not in u for u in _srcs(out))


def test_the_recommendation_strip_is_not_part_of_the_episode():
    out, _ = _inject()
    assert "other.jpg" not in out


def test_the_feed_image_is_removed_from_the_body():
    out, _ = _inject()
    assert FEED_IMG not in out


def test_a_page_with_no_slices_keeps_the_feed_image():
    out, lead = _inject(page="<html><body>nothing</body></html>")
    assert out == FEED_BODY and lead == FEED_IMG


def test_a_failed_fetch_keeps_the_feed_image():
    out, lead = _inject(page=None)
    assert out == FEED_BODY and lead == FEED_IMG


def test_other_hosts_are_untouched_and_never_fetched():
    with patch.object(main.lead_image_service, "fetch_source_html_now") as fetch:
        out, lead = main._inject_webtoons_episode_panels(
            FEED_BODY, _Entry("https://example.com/post"), "https://example.com/feed", FEED_IMG
        )
    assert out == FEED_BODY and lead == FEED_IMG
    fetch.assert_not_called()


def test_duplicate_slices_collapse():
    page = PAGE + f'<img class="_images" data-url="{SLICES[0]}">'
    out, _ = _inject(page=page)
    assert len(_srcs(out)) == 3


def test_the_slice_count_is_capped():
    many = "".join(
        f'<img class="_images" data-url="https://webtoon-phinf.pstatic.net/d/{i}.jpg">'
        for i in range(200)
    )
    out, _ = _inject(page=many)
    assert len(_srcs(out)) == main._WEBTOONS_MAX_PANELS


def test_a_single_slice_episode_still_renders_it():
    """Sarah's Scribbles: the one slice IS the feed's image. Harmless, and the
    body must not come back empty."""
    only = SLICES[0]
    page = f'<img class="_images" data-url="{only}">'
    out, _ = _inject(page=page)
    assert _srcs(out) == [main._webtoons_public_slice_url(only)]
