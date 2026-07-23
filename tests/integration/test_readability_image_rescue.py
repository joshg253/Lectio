"""Readability drops images the article needs; the whole-body last resort rescues
them — but only in the catastrophic case, never as routine widening.

guitarplayer lessons are the motivating example: readability kept ~1 of ~54
images and the dropped ones were the tablature figures that *are* the lesson, on
a DOM no content selector matches. These tests pin that a page shaped like that
recovers its images, while a page readability handles reasonably is left alone
(so its chrome images don't get dragged in)."""
from __future__ import annotations

import main

URL = "https://example.test/lesson"


def _imgs(n_content: int, n_chrome: int = 0, matchable: bool = False) -> str:
    """A page with n_content images buried where readability won't score them,
    plus n_chrome decorative images, and lots of prose so it extracts."""
    content = "".join(
        f'<div class="figrow"><img src="https://cdn.test/tab{i}.jpg" width="500"></div>'
        for i in range(n_content)
    )
    chrome = "".join(
        f'<img src="https://cdn.test/logo{i}.png" width="40">' for i in range(n_chrome)
    )
    prose = "".join(
        f"<p>Paragraph {i} of the lesson with enough words to read as an article "
        f"body rather than a caption or a stub sentence.</p>" for i in range(6)
    )
    # `matchable` puts the content in an entry-content div the selector fallback
    # finds; otherwise it's in bare divs no selector matches (the GP shape).
    wrap_open = '<div class="entry-content">' if matchable else "<div>"
    return (
        f"<html><head><title>Lesson</title></head><body>{chrome}"
        f"{wrap_open}{prose}{content}</div></body></html>"
    )


def test_rescues_images_when_readability_keeps_almost_none():
    # 20 content figures in bare divs; readability + selector fallback both miss
    # them. Whole-body rescue should bring them back.
    _title, html = main.extract_readability_article(_imgs(20), URL)
    assert html.lower().count("<img") >= 15


def test_leaves_a_reasonable_extraction_alone():
    """If readability (via the selector fallback) already keeps the content
    images, the whole-body last resort must not fire and drag in chrome."""
    # Content in entry-content (selector fallback finds it), 8 content imgs plus
    # 6 chrome imgs. The result should have the content, not balloon to include
    # every chrome logo.
    _title, html = main.extract_readability_article(_imgs(8, n_chrome=6, matchable=True), URL)
    count = html.lower().count("<img")
    assert count >= 8          # kept the content
    assert count < 8 + 6       # did NOT drag in all the chrome


def test_last_resort_needs_an_image_heavy_page():
    """A page with only a couple of images that readability drops is not the
    catastrophic case — the >10-image gate keeps the whole body out of it."""
    # 3 content images, none matchable. Below the raw>10 gate, so no whole-body
    # rescue; result stays small rather than pulling in the body.
    _title, html = main.extract_readability_article(_imgs(3), URL)
    assert html.lower().count("<img") <= 3
