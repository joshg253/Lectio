"""Tapas ships the wrong image for an article and the right one for a thumbnail.

`/sa/` is series art — one picture per episode, what the RSS feed carries. `/c/`
is the episode's actual content, one URL per panel, so a four-panel episode
arrives in the feed as a single image. Reported as "comic img is wrong, but good
for thumb".
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

import main

LINK = "https://tapas.io/episode/2415991"
SERIES_ART = "https://us-a.tapas.io/sa/34/811959d7-ad1c-47c0-8ee1-570d77204f48.jpg"
FEED_BODY = f'<p>null</p><img src="{SERIES_ART}"/>'
PANELS = [
    "https://us-a.tapas.io/c/51/671160c3-ccf4-4deb-ab1d-5b89de33d877.jpg?__token__=exp=1~acl=/c/51/a.jpg",
    "https://us-a.tapas.io/c/59/e8f654bc-b187-4381-9350-e3e959ea8879.jpg?__token__=exp=1~acl=/c/59/b.jpg",
    "https://us-a.tapas.io/c/a1/aeb9bd84-3708-4c09-bd3b-a946d00059a3.jpg?__token__=exp=1~acl=/c/a1/c.jpg",
    "https://us-a.tapas.io/c/f4/f7cdce51-1eb5-44e4-a5b9-a3af85518d29.jpg?__token__=exp=1~acl=/c/f4/d.jpg",
]
PAGE = "<html><body>" + "".join(f'<img src="{u}">' for u in PANELS) + \
       f'<img src="{SERIES_ART}"></body></html>'


class _Entry:
    def __init__(self, link=LINK):
        self.link = link
        self.feed_url = "https://tapas.io/rss/series/2007"
        self.id = LINK


def _inject(body, page=PAGE, entry=None):
    with patch.object(main.lead_image_service, "fetch_source_html_now",
                      return_value=(LINK, page) if page is not None else None):
        return main._inject_tapas_episode_panels(
            body, entry or _Entry(), "https://tapas.io/rss/series/2007", SERIES_ART
        )


def test_every_panel_lands_in_the_article_in_order():
    html_out, lead = _inject(FEED_BODY)
    assert html_out.count("<img") == 4, "a four-panel episode is four panels"
    assert [u.split("?")[0] for u in PANELS] == [
        s.split("?")[0] for s in __import__("re").findall(r'src="([^"]+)"', html_out)
    ]
    assert lead is None, "the hero is dropped or the thumbnail sits above its own comic"


def test_the_series_art_is_removed_from_the_body():
    html_out, _ = _inject(FEED_BODY)
    assert "/sa/" not in html_out


def test_the_null_paragraph_goes_too():
    """A Tapas body is literally `<p>null</p>` when the author wrote no caption."""
    html_out, _ = _inject(FEED_BODY)
    assert "null" not in html_out.lower()


def test_the_same_panel_under_a_different_token_is_not_repeated():
    dup = PAGE + f'<img src="{PANELS[0].split("?")[0]}?__token__=exp=999~acl=x">'
    html_out, _ = _inject(FEED_BODY, page=dup)
    assert html_out.count("<img") == 4


def test_a_body_that_already_has_the_panels_is_left_alone():
    body = f'<p><img src="{PANELS[0]}"/></p>'
    out, lead = _inject(body)
    assert out == body and lead == SERIES_ART


def test_a_page_with_no_panels_keeps_the_series_art():
    """Better the thumbnail than an article with no image at all."""
    out, lead = _inject(FEED_BODY, page="<html><body>nothing here</body></html>")
    assert out == FEED_BODY and lead == SERIES_ART


def test_a_failed_fetch_keeps_the_series_art():
    out, lead = _inject(FEED_BODY, page=None)
    assert out == FEED_BODY and lead == SERIES_ART


@pytest.mark.parametrize("link", [
    "https://example.com/post/1",
    "https://nottapas.io/episode/1",
])
def test_other_hosts_are_untouched_and_never_fetched(link):
    with patch.object(main.lead_image_service, "fetch_source_html_now") as fetch:
        out, lead = main._inject_tapas_episode_panels(
            FEED_BODY, _Entry(link), "https://example.com/feed", SERIES_ART
        )
    assert out == FEED_BODY and lead == SERIES_ART
    fetch.assert_not_called()


def test_the_panel_count_is_capped():
    many = "".join(
        f'<img src="https://us-a.tapas.io/c/{i:02d}/x{i}.jpg?__token__=exp=1">'
        for i in range(200)
    )
    out, _ = _inject(FEED_BODY, page=many)
    assert out.count("<img") == main._TAPAS_MAX_PANELS
