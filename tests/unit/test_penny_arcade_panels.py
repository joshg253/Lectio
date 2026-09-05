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


# ── The render path must not undo the resolution ─────────────────────────────
# Fixing the storage layer was not enough. _inject_webcomic_panel_into_bodyless_entry
# re-scanned the source page at render time for a bodyless webcomic entry and
# injected whatever it found — panel 1 — discarding the full strip that had just
# been resolved. Three places had to agree before the article was right.

def test_a_plugin_owned_host_is_not_rescanned_at_render_time(monkeypatch):
    import main

    scanned = []

    def _boom(link, is_webcomic=False):
        scanned.append(link)
        return "https://assets.penny-arcade.com/comics/panels/x-p1.jpg"

    monkeypatch.setattr(main.lead_image_service, "_fetch_source_lead_image", _boom)
    monkeypatch.setattr(main.lead_image_service, "_is_feed_webcomic", lambda _f: True)
    monkeypatch.setattr(
        main.lead_image_service, "_plugin_should_skip_source_lookup",
        lambda *, entry_link: True)

    entry = type("E", (), {"link": COMIC})()
    out, hero = main._inject_webcomic_panel_into_bodyless_entry("", entry, "f", FULL)

    assert FULL in out, "the article should carry the resolved full strip"
    assert not scanned, "a plugin-owned host must not be re-scanned on render"


def test_a_host_with_no_plugin_opinion_still_scans(monkeypatch):
    """mahonoir's case, which this injection was written for: the feed's own
    image is a share card, so the page really is the only source."""
    import main

    monkeypatch.setattr(main.lead_image_service, "_is_feed_webcomic", lambda _f: True)
    monkeypatch.setattr(
        main.lead_image_service, "_plugin_should_skip_source_lookup",
        lambda *, entry_link: False)
    monkeypatch.setattr(
        main.lead_image_service, "_fetch_source_lead_image",
        lambda link, is_webcomic=False: "https://cdn.example.com/real-comic.png")

    entry = type("E", (), {"link": "https://mahonoir.com/comic/1"})()
    out, hero = main._inject_webcomic_panel_into_bodyless_entry("", entry, "f", "https://cdn.example.com/share-card.png")

    assert "real-comic.png" in out
    assert hero is None


def test_a_body_that_already_had_its_own_image_is_not_rescanned(monkeypatch):
    """misfile.com's case: the body opens with the comic, _strip_lead_image_opener
    removes it as a duplicate of the already-resolved lead (correctly shown as the
    hero), leaving no <img> behind -- identical, post-strip, to mahonoir's body that
    never had one. Without body_had_image this re-fetched the source page for a
    panel that was already resolved, on every single open, racing the same-URL
    background fetch get_entry_detail's embed-recovery step already queued."""
    import main

    scanned = []

    def _boom(link, is_webcomic=False):
        scanned.append(link)
        return "https://www.misfile.com/comics/should-not-be-fetched.jpg"

    monkeypatch.setattr(main.lead_image_service, "_fetch_source_lead_image", _boom)
    monkeypatch.setattr(main.lead_image_service, "_is_feed_webcomic", lambda _f: True)
    monkeypatch.setattr(
        main.lead_image_service, "_plugin_should_skip_source_lookup",
        lambda *, entry_link: False)

    entry = type("E", (), {"link": "https://www.misfile.com/hell-high/9426"})()
    lead = "https://www.misfile.com/comics/1788467221-page1528.jpg"
    body = '<p><a href="https://www.misfile.com/hell-high/9426"><br/>New comic!</a><br/>Today\'s News:<br/></p>'
    out, hero = main._inject_webcomic_panel_into_bodyless_entry(
        body, entry, "f", lead, body_had_image=True)

    assert out == body, "the text-only body should be left exactly as-is"
    assert hero == lead, "the already-resolved lead must stay the hero, not be dropped"
    assert not scanned, "a body that already had its own image must not be re-scanned"
