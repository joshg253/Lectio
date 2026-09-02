"""_strip_feed_injected_blocks: per-feed removal of injected promo/sidebar div
blocks before lead-image extraction, so their thumbnails don't win the pick.

pcgamer.com/guitarplayer.com case found live 2026-09-01: Future plc's shared
CMS appends a <div class="product"><figure class="van-image-figure">...
<img.../></figure></a><p>2026 games: All the upcoming games<br/>...</p></div>
"related roundup" widget to most articles. A pcgamer.com post whose feed
content had exactly ONE <img> total — this widget's 654x661 uncaptioned
thumbnail — had it win the lead-image pick by default; the real 2345x1319
article photo never made it into the feed at all.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from services.lead_images import LeadImageService

_PCGAMER_WIDGET = (
    '<p>Real article text about the game.</p>'
    '<div class="product"><a><figure class="van-image-figure">'
    '<div class="image-full-width-wrapper"><div class="image-widthsetter">'
    '<p class="vanilla-image-block">'
    '<img align="middle" alt="" class="" height="654" '
    'src="https://cdn.mos.cms.futurecdn.net/6offQUY4CXebir2TC27dMd.jpg" width="661"/>'
    '</p></div></div></figure></a>'
    '<p><a href="https://www.pcgamer.com/games/new-pc-games-2026/"><strong>2026 games</strong></a>: '
    'All the upcoming games<br/>'
    '<a href="https://www.pcgamer.com/the-best-pc-games/"><strong>Best PC games</strong></a>: '
    'Our all-time favorites</p></div>'
)

_GUITARPLAYER_REAL_FIGURE = (
    '<p>Bring it up to tempo slowly.</p>'
    '<figure class="van-image-figure inline-layout"><div class="image-full-width-wrapper">'
    '<div class="image-widthsetter"><p class="vanilla-image-block">'
    '<img alt="Music tab for Guitar Players" src="https://cdn.mos.cms.futurecdn.net/real-tab.jpg"/>'
    '</p></div></div>'
    '<figcaption class="inline-layout"><span class="credit">(Image credit: Future)</span></figcaption>'
    '</figure>'
)


def _svc(tmp_path: Path) -> LeadImageService:
    def get_meta():
        c = sqlite3.connect(str(tmp_path / "m.sqlite"))
        c.row_factory = sqlite3.Row
        return c

    return LeadImageService(
        get_meta_connection=get_meta,
        get_reader=lambda: None,
        user_agent="LectioTest/1.0",
        extract_video_id=lambda link: None,
    )


def test_pcgamer_product_widget_is_stripped(tmp_path):
    svc = _svc(tmp_path)
    out = svc._strip_feed_injected_blocks(_PCGAMER_WIDGET, "https://www.pcgamer.com/feeds.xml")
    assert "6offQUY4CXebir2TC27dMd" not in out
    assert "<img" not in out
    assert "2026 games" not in out  # the roundup-links paragraph goes too
    assert "Real article text about the game." in out


def test_guitarplayer_product_widget_is_stripped(tmp_path):
    svc = _svc(tmp_path)
    out = svc._strip_feed_injected_blocks(_PCGAMER_WIDGET, "https://www.guitarplayer.com/feeds/tag/lessons")
    assert "6offQUY4CXebir2TC27dMd" not in out


def test_van_image_figure_without_product_wrapper_survives(tmp_path):
    """The strip is scoped to class="product" specifically, not the shared
    van-image-figure component Future's CMS also uses for genuine content —
    a real captioned guitar-tab image (guitarplayer.com) must not be eaten."""
    svc = _svc(tmp_path)
    out = svc._strip_feed_injected_blocks(_GUITARPLAYER_REAL_FIGURE, "https://www.guitarplayer.com/feeds/tag/lessons")
    assert "real-tab.jpg" in out
    assert "Image credit: Future" in out


def test_unrelated_host_is_unaffected(tmp_path):
    svc = _svc(tmp_path)
    out = svc._strip_feed_injected_blocks(_PCGAMER_WIDGET, "https://example.com/feed")
    assert "6offQUY4CXebir2TC27dMd" in out
