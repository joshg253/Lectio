"""Attachments list: local copies win, and captured files join the same list.

A saved post whose "download the tab" link still points at a dead publisher has
kept the wrong half, so anything archived is served from the local copy.
"""
from __future__ import annotations

import pytest

import main
from services.starred_archive import StarredArchiveService


class _Enc:
    def __init__(self, href, type_="", length=None):
        self.href = href
        self.type = type_
        self.length = length


class _Entry:
    feed_url = "https://standardebooks.org/rss/new-releases"
    id = "https://standardebooks.org/ebooks/joseph-conrad/suspense"

    def __init__(self, enclosures):
        self.enclosures = enclosures


EPUB = "https://standardebooks.org/ebooks/x/downloads/book.epub"
TAB = "https://assets-wp.guitar-pro.eu/x/song.gp"


def test_an_archived_enclosure_is_served_locally():
    html = main._render_entry_attachments(_Entry([_Enc(EPUB, "application/epub+zip")]),
                                          None, {EPUB: "HASH1"})
    assert "/starred-asset/HASH1" in html
    assert EPUB not in html
    assert "saved" in html


def test_an_unarchived_enclosure_still_links_to_the_publisher():
    html = main._render_entry_attachments(_Entry([_Enc(EPUB, "application/epub+zip")]),
                                          None, {})
    assert EPUB in html
    assert "/starred-asset/" not in html


def test_captured_body_files_join_the_same_list(monkeypatch):
    """From the reader's side these are the same thing — "files that came with
    this post" — and the difference is only how Lectio found them."""
    monkeypatch.setattr(main.starred_archive_service, "get_entry_file_assets",
                        lambda f, e: {TAB: "HASH2"})

    html = main._render_entry_attachments(_Entry([]), None, {TAB: "HASH2"})

    assert "/starred-asset/HASH2" in html
    assert "Attachments" in html


def test_images_and_audio_are_not_listed_as_attachments(monkeypatch):
    """Decided by STORED content type, not by the URL: a Gravatar
    ("/avatar/<hash>?s=48") is an image with nothing in its URL to say so, and
    two of them surfaced as "webp attachments"."""
    # get_entry_file_assets is what applies the content-type filter, so an
    # image never reaches the renderer at all.
    monkeypatch.setattr(main.starred_archive_service, "get_entry_file_assets",
                        lambda f, e: {})

    html = main._render_entry_attachments(_Entry([]), None, {
        "https://x.test/photo.jpg": "H1",
        "https://x.test/show.mp3": "H2",
    })

    assert html == ""


def test_an_enclosure_is_not_listed_twice():
    html = main._render_entry_attachments(_Entry([_Enc(EPUB, "application/epub+zip")]),
                                          None, {EPUB: "HASH1"})
    assert html.count("<li>") == 1


def test_nothing_renders_when_there_is_nothing():
    assert main._render_entry_attachments(_Entry([]), None, {}) == ""


# --- body links point at the saved copy -------------------------------------


def _rewrite(html, asset_map):
    svc = StarredArchiveService.__new__(StarredArchiveService)
    return svc.rewrite_html_assets(html, asset_map, "/starred-asset/")


def test_a_saved_file_link_is_rewired():
    out = _rewrite('<a href="https://x.test/song.gp">tab</a>', {"https://x.test/song.gp": "H"})
    assert 'href="/starred-asset/H"' in out


def test_an_ordinary_link_is_untouched():
    """Only URLs actually in the map are touched, so this can only ever
    redirect a link to a file we hold."""
    html = '<a href="https://x.test/article">read</a>'
    assert _rewrite(html, {"https://x.test/song.gp": "H"}) == html


def test_images_are_still_rewritten():
    out = _rewrite('<img src="https://x.test/a.png">', {"https://x.test/a.png": "H"})
    assert 'src="/starred-asset/H"' in out


def test_both_in_one_document():
    out = _rewrite(
        '<a href="https://x.test/song.gp">t</a><img src="https://x.test/a.png">',
        {"https://x.test/song.gp": "H1", "https://x.test/a.png": "H2"},
    )
    assert 'href="/starred-asset/H1"' in out
    assert 'src="/starred-asset/H2"' in out


# --- which enclosures are captured -------------------------------------------
#
# Enclosures are kept WITHOUT the per-feed extension list, because an
# <enclosure> is the publisher declaring the file belongs to the post. These pin
# the selection rule the capture path applies.


def _capturable(enclosures) -> list[str]:
    """Mirrors the enclosure filter in StarredArchiveService._archive_entry."""
    out = []
    for enc in enclosures:
        url = (enc.href or "").strip()
        if not url:
            continue
        if (enc.type or "").lower().startswith(("audio/", "image/")):
            continue
        out.append(url)
    return out


def test_an_epub_enclosure_is_captured():
    """Standard Ebooks attaches the book itself — needing per-feed setup for
    that would be asking the user to state the obvious."""
    epub = "https://standardebooks.org/ebooks/x/downloads/book.epub?source=feed"
    assert _capturable([_Enc(epub, "application/epub+zip", 554861)]) == [epub]


def test_a_query_string_does_not_stop_an_enclosure_being_captured():
    """Standard Ebooks appends ?source=feed to every enclosure URL."""
    assert _capturable([_Enc("https://x.test/b.epub?source=feed", "application/epub+zip")])


def test_audio_enclosures_are_skipped():
    """Podcast enclosures are large and stream fine, and they already have a
    player."""
    assert _capturable([_Enc("https://x.test/ep.mp3", "audio/mpeg")]) == []


def test_image_enclosures_are_skipped():
    """Already captured as images; listing them again poisons the lead-image
    dedup."""
    assert _capturable([_Enc("https://x.test/cover.jpg", "image/jpeg")]) == []


def test_a_pdf_enclosure_is_captured():
    """Magazine feeds attach the issue."""
    assert _capturable([_Enc("https://x.test/issue.pdf", "application/pdf")])


# --- what is NOT an attachment ----------------------------------------------


def test_a_share_button_is_not_an_image_link():
    """A Pinterest "/pin/create/button/?…&media=….jpg" link ENDS in .jpg, so a
    pattern anchored on the whole href matched it — and 1.1MB of HTML was
    fetched and stored as an asset. The URL PATH is what decides."""
    svc = StarredArchiveService.__new__(StarredArchiveService)
    html = ('<a href="https://pinterest.com/pin/create/button/'
            '?url=https%3A%2F%2Fx.test%2Fp&media=https%3A%2F%2Fx.test%2Fa.jpg">pin</a>'
            '<a href="https://x.test/real.jpg">photo</a>')

    got = svc._extract_image_urls(html, "https://x.test/")

    assert got == {"https://x.test/real.jpg"}


def test_an_image_link_with_a_query_still_counts():
    """Only the path decides — a cache-buster must not disqualify a real image."""
    svc = StarredArchiveService.__new__(StarredArchiveService)
    got = svc._extract_image_urls('<a href="https://x.test/a.png?v=2">i</a>', "https://x.test/")
    assert got == {"https://x.test/a.png?v=2"}


# --- downloads must arrive with a usable name --------------------------------
#
# An archived asset is addressed by CONTENT HASH, so without a filename the
# browser saves "cfc24ad676575660aa54d641afe8b2c86e8fa02bc…" with no extension —
# unopenable and unidentifiable.


@pytest.mark.parametrize("url,expected", [
    ("https://assets-wp.guitar-pro.eu/uploads/2024/12/Melody-Danny_Boy.gp",
     "Melody-Danny_Boy.gp"),
    ("https://x.test/a%20b/My%20Tab.gp5?v=2", "My Tab.gp5"),   # decoded, query dropped
    ('https://x.test/na:me*.pdf', "na_me_.pdf"),               # sanitized
    ("https://x.test/no-basename/", "attachment"),             # nothing to use
])
def test_download_filename_is_derived_from_the_source_url(url, expected):
    assert main.attachment_filename_for_url(url) == expected


def test_the_attachment_link_carries_the_filename():
    html = main._render_entry_attachments(
        _Entry([_Enc(EPUB, "application/epub+zip")]), None, {EPUB: "HASH1"})
    assert 'download="book.epub"' in html


def test_a_rewritten_body_link_carries_the_filename():
    tab = "https://x.test/uploads/Melody-Danny_Boy.gp"
    out = _rewrite(f'<a href="{tab}">tab</a>', {tab: "H"})
    assert 'download="Melody-Danny_Boy.gp"' in out
    assert 'href="/starred-asset/H"' in out


def test_an_existing_download_attribute_is_left_alone():
    """The publisher's own chosen name wins over ours."""
    tab = "https://x.test/a.gp"
    out = _rewrite(f'<a href="{tab}" download="Their Name.gp">t</a>', {tab: "H"})
    assert out.count("download=") == 1
    assert "Their Name.gp" in out
