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


def test_text_article_on_a_chrome_heavy_page_is_not_widened():
    """A plain text article (no images) whose page is image-heavy only from nav/
    footer chrome must keep readability's clean extraction. The whole-body rescue
    fires on ≤1 kept image + >10 present, but here that would replace a real
    article with the entire nav-laden page — the Google Developers Blog case. The
    text-length guard keeps it out."""
    chrome = "".join(f'<img src="https://cdn.test/navicon{i}.png" width="16">' for i in range(20))
    prose = "".join(
        f"<p>Paragraph {i}: {'the future of java 8 language features on android ' * 6}</p>"
        for i in range(8)
    )
    page = (
        "<html><head><title>Java 8</title></head><body>"
        f"<header><nav>{chrome}</nav></header>"
        f'<div class="post">{prose}</div>'
        f"<footer>{chrome}</footer>"
        "</body></html>"
    )
    _title, html = main.extract_readability_article(page, URL)
    assert "future of java" in html.lower()          # the real article survived
    assert "navicon0.png" not in html                # chrome not dragged in
    assert html.lower().count("<img") <= 2           # not the 40 nav/footer icons


def test_last_resort_needs_an_image_heavy_page():
    """A page with only a couple of images that readability drops is not the
    catastrophic case — the >10-image gate keeps the whole body out of it."""
    # 3 content images, none matchable. Below the raw>10 gate, so no whole-body
    # rescue; result stays small rather than pulling in the body.
    _title, html = main.extract_readability_article(_imgs(3), URL)
    assert html.lower().count("<img") <= 3


def test_future_plc_article_body_id_beats_whole_body():
    """Future plc sites (guitarplayer/guitarworld/musicradar) put the article in
    an id=article-body container. The fallback must pick that — clean tab
    figures — over the whole body, which drags in related-article chrome that
    made captures 'pull in a bunch of bs'."""
    tabs = "".join(f'<img src="https://cdn.test/tab{i}.jpg" width="600">' for i in range(15))
    chrome_imgs = "".join(f'<img src="https://cdn.test/related{i}.jpg">' for i in range(20))
    page = (
        "<html><head><title>Arpeggios</title></head><body>"
        f'<header><nav>{chrome_imgs}</nav></header>'
        '<div id="article-body">'
        "<p>Learn arpeggios with these tab exercises over the next hour of practice.</p>"
        f"{tabs}</div>"
        f'<aside class="related">More from GuitarPlayer{chrome_imgs}</aside>'
        "</body></html>"
    )
    _title, html = main.extract_readability_article(page, URL)
    assert 12 <= html.lower().count("<img") <= 16      # the tabs, not the chrome
    assert "related0.jpg" not in html                   # no related-article junk
    assert "tab0.jpg" in html


def test_future_plc_in_body_chrome_is_stripped():
    """Future plc nests a social-share bar and a newsletter signup *inside*
    #article-body, ahead of the prose, so a container match kept them and every
    capture led with 'Facebook X Pinterest … Subscribe to our newsletter'. The
    chrome strip drops the widgets while leaving the lesson and its tab figures."""
    tabs = "".join(f'<img src="https://cdn.test/tab{i}.jpg" width="600">' for i in range(15))
    page = (
        "<html><head><title>Turbo Slurs</title></head><body>"
        '<div id="article-body">'
        '<div class="flexisites-social"><a>Facebook</a><a>Pinterest</a>Share this article</div>'
        '<a class="google-follow-us-button">Follow us</a>'
        "<p>You don't have to be fast to be a great guitarist, but it helps to fake it.</p>"
        f"{tabs}"
        '<aside class="related">You may like: Joe Satriani on small realities</aside>'
        '<div class="slice-container newsletter-inbodyContent-slice">Subscribe to our newsletter</div>'
        "</div></body></html>"
    )
    _title, html = main.extract_readability_article(page, URL)
    assert "great guitarist" in html                    # the article stayed
    assert "tab0.jpg" in html                            # the tab figures stayed
    assert "Pinterest" not in html                       # share bar gone
    assert "Subscribe to our newsletter" not in html     # newsletter gone
    assert "You may like" not in html                    # related aside gone
    assert "Follow us" not in html


def test_future_plc_video_carousel_card_is_stripped():
    """The JW Player 'Latest Videos From … Watch full video here:' carousel sits
    in an unsemantic rounded card that also holds a header bar and an invisible
    watch-here link. Climbing from the player marker to the card removes all of
    it, while a real tab figure outside the card survives."""
    tabs = "".join(f'<img src="https://cdn.test/tab{i}.jpg" width="600">' for i in range(15))
    page = (
        "<html><head><title>Slurs</title></head><body>"
        '<div id="article-body">'
        "<p>Hammer-ons and pull-offs let you sound fast without playing fast.</p>"
        '<div class="my-6 w-full overflow-hidden rounded-[10px]">'
        '  <div class="jwp-carousel-title-desktop">Latest Videos From</div>'
        '  <div class="aspect-video"><img src="https://cdn.test/vidthumb.jpg"></div>'
        '  <div class="bg-zinc-900"><a class="invisible">Watch full video here:</a></div>'
        "</div>"
        f"{tabs}</div></body></html>"
    )
    _title, html = main.extract_readability_article(page, URL)
    assert "Latest Videos From" not in html
    assert "Watch full video here" not in html
    assert "vidthumb.jpg" not in html      # the video's own thumbnail went with it
    assert "tab0.jpg" in html              # the lesson's tab figures stayed


def test_lead_image_prepended_from_og_when_absent():
    """A site's hero image lives in the page header, outside the content
    readability extracts, so captures lost it. og:image is prepended when the
    body doesn't already open with it — but a logo/svg og:image is skipped."""
    body = (
        "<div id='article-body'>"
        "<p>The article body has plenty of prose to extract cleanly here.</p>"
        '<img src="https://cdn.test/inline.jpg" width="500">'
        "</div>"
    )
    page = (
        '<html><head><meta property="og:image" '
        'content="https://cdn.test/hero-700-80.png"></head>'
        f"<body>{body}</body></html>"
    )
    _title, html = main.extract_readability_article(page, URL)
    assert "hero-700-80.png" in html
    assert html.find("hero-700-80.png") < html.find("inline.jpg")  # hero leads

    # A logo og:image must NOT be stamped onto the capture.
    logo_page = page.replace("hero-700-80.png", "site-logo.png")
    _t, logo_html = main.extract_readability_article(logo_page, URL)
    assert "site-logo.png" not in logo_html
