"""A webcomic wants a different image in the list than in the article.

A Penny Arcade strip is ~1050x438 — three panels side by side. That is the
article image, and it is three unreadable smudges at thumbnail size; panel 1 is
legible. Both live at derivable paths, so no extra fetch is needed:

    article : …/comics/<hash>.jpg
    panel 1 : …/comics/panels/<hash>-p1.jpg

Before this, the entry had *neither*: the source-page scan stored panel 1 as the
article image (the plugin never declared should_skip_source_lookup, so its
og:image fallback never ran), and the panel-bypass then returned None for the
thumbnail rather than falling back — so the list showed nothing at all.
"""
from __future__ import annotations

import pytest

from services.lead_image_plugins import PennyArcadePlugin

COMIC = "https://www.penny-arcade.com/comic/2026/08/10/passionate-intensity"
NEWS = "https://www.penny-arcade.com/news/post/2026/08/10/something"
FULL = "https://assets.penny-arcade.com/comics/20260810-gRi7ErAS.jpg"
PANEL1 = "https://assets.penny-arcade.com/comics/panels/20260810-gRi7ErAS-p1.jpg"


@pytest.fixture
def plugin():
    return PennyArcadePlugin()


def test_the_full_strip_thumbnails_as_panel_one(plugin):
    assert plugin.thumbnail_from_lead_image(entry_link=COMIC, lead_url=FULL) == PANEL1


def test_a_panel_is_already_a_fine_thumbnail(plugin):
    """Older cached values are panels; they must not become None."""
    assert plugin.thumbnail_from_lead_image(entry_link=COMIC, lead_url=PANEL1) == PANEL1


def test_the_source_scan_is_skipped_for_comics(plugin):
    """The page's first <img> is panel 1, so scanning it beats the og:image
    fallback to the punch — which is the bug that left the article showing a
    single panel instead of the strip."""
    assert plugin.should_skip_source_lookup(entry_link=COMIC) is True


def test_non_comic_entries_are_left_alone(plugin):
    assert plugin.should_skip_source_lookup(entry_link=NEWS) is False
    assert plugin.thumbnail_from_lead_image(entry_link=NEWS, lead_url=FULL) is None


def test_other_hosts_are_not_rewritten(plugin):
    assert plugin.thumbnail_from_lead_image(
        entry_link=COMIC, lead_url="https://example.com/comics/x.jpg") is None


@pytest.mark.parametrize("ext", [".jpg", ".jpeg", ".png", ".webp"])
def test_each_image_extension_is_handled(plugin, ext):
    out = plugin.thumbnail_from_lead_image(
        entry_link=COMIC, lead_url=f"https://assets.penny-arcade.com/comics/abc{ext}")
    assert out == f"https://assets.penny-arcade.com/comics/panels/abc-p1{ext}"


def test_a_url_that_is_not_a_comic_image_is_ignored(plugin):
    assert plugin.thumbnail_from_lead_image(
        entry_link=COMIC,
        lead_url="https://assets.penny-arcade.com/img/avatars/avatar-tycho.jpg") is None
