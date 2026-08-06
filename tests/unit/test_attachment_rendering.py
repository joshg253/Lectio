"""Attachments list: local copies win, and captured files join the same list.

A saved post whose "download the tab" link still points at a dead publisher has
kept the wrong half, so anything archived is served from the local copy.
"""
from __future__ import annotations

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


def test_captured_body_files_join_the_same_list():
    """From the reader's side these are the same thing — "files that came with
    this post" — and the difference is only how Lectio found them."""
    html = main._render_entry_attachments(_Entry([]), None, {TAB: "HASH2"})
    assert "/starred-asset/HASH2" in html
    assert "Attachments" in html


def test_images_and_audio_are_not_listed_as_attachments():
    """Images render inline and audio has its own player."""
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
