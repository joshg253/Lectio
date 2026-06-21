"""_render_entry_attachments surfaces non-audio enclosures (magazine PDFs,
EPUBs, etc.) as a footer download list. Audio enclosures are excluded because
they're already shown as an inline player, and placeholder sizes are dropped."""
from __future__ import annotations

from types import SimpleNamespace

import main


def _enc(href, type=None, length=None):
    return SimpleNamespace(href=href, type=type, length=length)


def _entry(enclosures=()):
    return SimpleNamespace(enclosures=list(enclosures))


def test_pdf_and_epub_enclosures_listed():
    e = _entry([
        _enc("https://dl.test/issue229_en.pdf", type="application/pdf"),
        _enc("https://dl.test/issue229_en.epub", type="application/epub+zip"),
    ])
    html = main._render_entry_attachments(e, None)
    assert "Attachments" in html
    assert "issue229_en.pdf" in html
    assert "issue229_en.epub" in html
    assert 'href="https://dl.test/issue229_en.pdf"' in html


def test_no_enclosures_returns_empty():
    assert main._render_entry_attachments(_entry([]), None) == ""


def test_audio_enclosure_excluded():
    e = _entry([_enc("https://cdn.test/ep1.mp3", type="audio/mpeg")])
    assert main._render_entry_attachments(e, "https://cdn.test/ep1.mp3") == ""


def test_resolved_audio_url_excluded_even_without_extension():
    e = _entry([_enc("https://cdn.test/ep1.bin", type="audio/mpeg")])
    assert main._render_entry_attachments(e, "https://cdn.test/ep1.bin") == ""


def test_image_enclosure_excluded_by_type():
    # Image enclosures are the post's lead/inline image (e.g. gottadeal), not a
    # download — surfaced as the lead image instead of an attachment link.
    e = _entry([_enc("https://cdn.test/photo.bin", type="image/jpeg")])
    assert main._render_entry_attachments(e, None) == ""


def test_image_enclosure_excluded_by_extension():
    e = _entry([_enc("https://gottadeal.s3.amazonaws.com/posts/123x250x243.jpg")])
    assert main._render_entry_attachments(e, None) == ""


def test_duplicate_urls_collapsed():
    e = _entry([
        _enc("https://dl.test/a.pdf", type="application/pdf"),
        _enc("https://dl.test/a.pdf", type="application/pdf"),
    ])
    assert main._render_entry_attachments(e, None).count("<li>") == 1


def test_placeholder_size_dropped_real_size_kept():
    assert main._format_enclosure_size(1024) == ""
    assert main._format_enclosure_size(None) == ""
    assert main._format_enclosure_size(5 * 1024 * 1024) == "5.0 MB"


def test_label_falls_back_to_type_when_no_filename():
    assert main._enclosure_label("https://dl.test/", "application/pdf") == "application/pdf"


def test_url_is_escaped():
    e = _entry([_enc('https://dl.test/a.pdf?x="evil', type="application/pdf")])
    html = main._render_entry_attachments(e, None)
    assert '"evil' not in html.replace("&quot;evil", "")
