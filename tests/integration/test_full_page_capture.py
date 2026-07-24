"""Full-page capture: the escape hatch for pages readability handles badly.

Readability makes editorial decisions that are wrong for some page shapes — a
DocBook-style export whose prose is scattered across low-scoring sibling divs
(so a single <pre> wins), or a lead image in a text-free floated div that its
cleaning drops (the Blood Meridian case). Those are structural, so re-running
readability just reproduces them. Full-page capture takes the whole body
instead. These tests pin the two behaviors that matter: it keeps what
readability drops, and it still sanitizes."""
from __future__ import annotations

import main

URL = "https://example.test/article"


# A page whose lead image sits in a text-free floated div — exactly the shape
# readability's conditional cleaning strips, dropping the cover art.
LEAD_IMAGE_PAGE = """
<html><head><title>An Article With A Cover</title></head><body>
<article>
  <div class="separator" style="float:left"><a href="/big.jpg">
    <img src="https://cdn.example.test/cover.jpg" width="272" height="400"></a></div>
  <p>It's a familiar story. The prose begins here and runs for several
     paragraphs about the topic at hand, with enough words to be a real article
     body that readability would happily keep on its own.</p>
  <p>A second paragraph continues the discussion so the page is unambiguously
     an article rather than a stub.</p>
</article>
</body></html>
"""

# DocBook-style: prose split across sibling divs, one big <pre> that readability
# scores highest, no <article>/<section>.
DOCBOOK_PAGE = """
<html><head><title>Everyday Git</title></head><body>
<div><p>Introduction paragraph describing what this guide covers in prose.</p></div>
<div><pre>$ git status\n$ git commit -m one\n$ git push origin main</pre></div>
<div><p>A second prose paragraph that a reader actually wants to keep.</p></div>
<div><p>A third prose paragraph, again real article text worth capturing.</p></div>
</body></html>
"""

DANGEROUS_PAGE = """
<html><head><title>Has Script</title></head><body>
<nav>site nav</nav>
<article>
  <p>Real content that should survive.</p>
  <script>alert('xss')</script>
  <img src="javascript:alert(1)">
</article>
<footer>site footer</footer>
</body></html>
"""


def test_keeps_a_lead_image_readability_would_drop():
    title, html = main.extract_full_page_article(LEAD_IMAGE_PAGE, URL)
    assert title == "An Article With A Cover"
    assert "cover.jpg" in html
    assert "familiar story" in html


def test_keeps_docbook_prose_scattered_across_divs():
    title, html = main.extract_full_page_article(DOCBOOK_PAGE, URL)
    assert title == "Everyday Git"
    # All three prose paragraphs survive, not just the <pre> readability favors.
    assert "Introduction paragraph" in html
    assert "second prose paragraph" in html
    assert "third prose paragraph" in html


def test_still_sanitizes_dangerous_content():
    _title, html = main.extract_full_page_article(DANGEROUS_PAGE, URL)
    assert "Real content that should survive" in html
    assert "<script" not in html.lower()
    assert "javascript:" not in html.lower()


def test_strips_obvious_non_content_chrome():
    _title, html = main.extract_full_page_article(DANGEROUS_PAGE, URL)
    # nav/header/footer are removed even on a document page — never the article.
    assert "site nav" not in html
    assert "site footer" not in html


def test_title_prefers_og_title():
    page = ('<html><head><title>Tab Title</title>'
            '<meta property="og:title" content="Social Title"></head>'
            '<body><p>Body text goes here for the capture.</p></body></html>')
    title, _html = main.extract_full_page_article(page, URL)
    assert title == "Social Title"


def test_title_falls_back_to_the_url():
    page = "<html><body><p>Body with no title anywhere on the page.</p></body></html>"
    title, _html = main.extract_full_page_article(page, URL)
    assert title == URL


def test_relative_image_urls_are_absolutized():
    page = ('<html><head><title>T</title></head><body>'
            '<p>Words that make this a real body worth keeping around.</p>'
            '<img src="/media/pic.png"></body></html>')
    _title, html = main.extract_full_page_article(page, "https://example.test/dir/post")
    assert "https://example.test/media/pic.png" in html


def test_empty_page_raises():
    import pytest
    with pytest.raises(ValueError):
        main.extract_full_page_article("<html><body></body></html>", URL)


def test_full_page_keeps_more_than_readability_on_the_hard_case():
    """The whole point: on the lead-image page, full capture retains the image
    that readability drops."""
    _t, full = main.extract_full_page_article(LEAD_IMAGE_PAGE, URL)
    _t2, readable = main.extract_readability_article(LEAD_IMAGE_PAGE, URL)
    assert "cover.jpg" in full
    # Document the contrast rather than asserting readability's exact behavior:
    # full capture never has fewer images than readability's output.
    assert full.lower().count("<img") >= readable.lower().count("<img")
