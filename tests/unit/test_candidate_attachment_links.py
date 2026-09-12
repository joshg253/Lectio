"""candidate_attachment_links_in_html finds file-like links regardless of any
feed's attachment-extension policy -- the per-entry Attachments panel's
"available to save individually" list, for a file a feed's chosen extensions
don't happen to cover."""
from __future__ import annotations

import main


def test_finds_a_pdf_link():
    html = '<p>See the <a href="https://cdn.test/manual.pdf">manual</a>.</p>'
    assert main.candidate_attachment_links_in_html(html, "https://example.test/post") == [
        "https://cdn.test/manual.pdf"
    ]


def test_resolves_relative_links_against_base_url():
    html = '<a href="/files/tab.gp5">tab</a>'
    assert main.candidate_attachment_links_in_html(html, "https://example.test/post") == [
        "https://example.test/files/tab.gp5"
    ]


def test_ignores_page_extensions():
    html = '<a href="https://example.test/about.html">about</a>'
    assert main.candidate_attachment_links_in_html(html, "https://example.test/post") == []


def test_ignores_images_already_covered_by_the_archives_own_image_scan():
    html = '<a href="https://cdn.test/photo.jpg">photo</a>'
    assert main.candidate_attachment_links_in_html(html, "https://example.test/post") == []


def test_ignores_bare_domain_tld_lookalikes():
    html = '<a href="https://example.com">home</a>'
    assert main.candidate_attachment_links_in_html(html, "https://example.test/post") == []


def test_query_string_does_not_turn_a_page_into_a_file():
    html = '<a href="https://example.test/post.php?file=x.pdf">page</a>'
    assert main.candidate_attachment_links_in_html(html, "https://example.test/post") == []


def test_finds_obfuscated_base64_links_too():
    import base64

    encoded = base64.b64encode(b"https://cdn.test/hidden.epub").decode()
    html = f'<span data-o="{encoded}">Download</span>'
    assert main.candidate_attachment_links_in_html(html, "https://example.test/post") == [
        "https://cdn.test/hidden.epub"
    ]


def test_dedupes_repeated_links():
    html = (
        '<a href="https://cdn.test/a.pdf">one</a>'
        '<a href="https://cdn.test/a.pdf">two</a>'
    )
    assert main.candidate_attachment_links_in_html(html, "https://example.test/post") == [
        "https://cdn.test/a.pdf"
    ]


def test_empty_html_returns_nothing():
    assert main.candidate_attachment_links_in_html("", "https://example.test/post") == []
    assert main.candidate_attachment_links_in_html(None, "https://example.test/post") == []
