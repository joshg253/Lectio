"""Comment threads defeat extraction and must be stripped before it runs.

A blogspot/WordPress comments section carries hundreds of avatar/delete-icon
images and a wall of text, so readability scores it above the actual post — and
the "more images = better" fallback can't rescue it, because the comment avatars
outnumber the real body's images (a webcomic post-body has ~19 panels vs ~440
comment avatars). These tests pin that comment containers are removed and the
real body survives, without touching content that merely mentions comments."""
from __future__ import annotations

import re

import main

URL = "https://blog.test/post"


def _comic_page(comment_imgs: int) -> str:
    panels = "".join(
        f'<img src="https://cdn.test/panel{i}.jpg" width="600">' for i in range(19)
    )
    avatars = "".join(
        f'<img src="https://cdn.test/avatar{i}.gif" width="35">' for i in range(comment_imgs)
    )
    return (
        "<html><head><title>Homeopathy</title></head><body>"
        '<div class="post-body entry-content">'
        "<p>A short webcomic with almost no prose, just panels.</p>"
        f"{panels}</div>"
        f'<div id="comments" class="comments"><h3>{comment_imgs} comments</h3>'
        f"<p>Endless argument in the thread.</p>{avatars}</div>"
        "</body></html>"
    )


def test_strip_removes_the_comments_container():
    html = _comic_page(comment_imgs=440)
    stripped = main._strip_comment_sections(html)
    assert "Endless argument" not in stripped
    assert stripped.lower().count("<img") == 19  # panels only


def test_extraction_keeps_the_comic_not_the_comments():
    """The end-to-end case: readability would grab the 440-avatar comments; with
    them stripped it scores the 19-panel body instead."""
    _title, html = main.extract_readability_article(_comic_page(comment_imgs=440), URL)
    assert html.lower().count("<img") >= 15
    assert "panel0.jpg" in html
    assert "Endless argument" not in html
    assert "avatar0.gif" not in html


def test_full_page_capture_also_drops_comments():
    _title, html = main.extract_full_page_article(_comic_page(comment_imgs=440), URL)
    assert "panel0.jpg" in html
    assert "Endless argument" not in html


def test_disqus_and_respond_are_stripped():
    html = (
        "<html><body><article><p>Real body text of the post goes here with words.</p>"
        '<img src="https://cdn.test/hero.jpg"></article>'
        '<div id="disqus_thread"><p>disqus comments</p></div>'
        '<div id="respond"><form>leave a reply</form></div></body></html>'
    )
    stripped = main._strip_comment_sections(html)
    assert "disqus comments" not in stripped
    assert "leave a reply" not in stripped
    assert "Real body text" in stripped


def test_content_that_merely_mentions_comments_is_kept():
    """No broad [class*=comment] matching — an article *about* comments, or a
    'N comments' badge, must not be deleted."""
    html = (
        "<html><body><article class=\"commentary\">"
        "<p>This essay is a commentary on internet comment culture.</p>"
        '<span class="comment-count">42 comments</span>'
        "</article></body></html>"
    )
    stripped = main._strip_comment_sections(html)
    assert "commentary on internet comment culture" in stripped
    assert "42 comments" in stripped


def test_no_comments_section_is_a_passthrough():
    html = "<html><body><article><p>Just an article, no comments at all here.</p></article></body></html>"
    assert main._strip_comment_sections(html) == html
