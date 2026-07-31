"""_strip_lead_image_opener dedups the lead image against the article body —
opener strip, mid-article suppression, artwork hoist, Tumblr size-variant dedup,
and thumbnail-wrapper stripping. Extracted from get_entry_detail."""
from __future__ import annotations

import main

LEAD = "https://cdn.test/photo.jpg"


def _strip(content, lead=LEAD, feed="https://f.test/feed", show=True):
    return main._strip_lead_image_opener(content, lead, feed, show)


def test_hidden_lead_still_strips_the_body_opener():
    """This deliberately overturns the previous assertion that show=False was a
    complete no-op.

    "Don't show the lead image in the article" has to include the case where the
    FEED put it there. The old behavior suppressed only Lectio's own copy above
    the article, so unchecking the box did nothing visible on a feed whose body
    opens with the same image — reported live on Blogger, which emits its
    social-preview asset (…-Blog-Metadata.png) as a real <img> at the top of the
    post body. That asset is also what the scorer picks as the thumbnail, so the
    article opened with a near-identical crop of its own first picture.
    """
    content, lead = _strip(f'<p><img src="{LEAD}"></p><p>body</p>', show=False)
    assert LEAD not in (content or "")
    assert "body" in content
    assert lead == LEAD          # still the thumbnail; only the body copy goes


def test_hidden_lead_leaves_a_mid_article_occurrence_alone():
    """Only the opener is a header. The same image further down is the author
    placing it in the flow, which is content."""
    html = f'<p>intro text</p><p><img src="{LEAD}"></p><p>more</p>'
    content, lead = _strip(html, show=False)
    assert content == html
    assert lead == LEAD


def test_hidden_lead_with_no_lead_image_is_a_noop():
    html = "<p>just text</p>"
    assert _strip(html, lead=None, show=False) == (html, None)


def test_opener_equals_lead_is_stripped(monkeypatch):
    monkeypatch.setattr(main.lead_image_service, "get_feed_strategy", lambda u: ("auto", 0.0, False))
    content, lead = _strip(f'<figure><img src="{LEAD}"></figure><p>real body text</p>')
    assert "real body text" in content
    assert LEAD not in content        # opener removed from body
    assert lead == LEAD               # still shown at top


def test_lead_buried_midarticle_drops_separate_lead(monkeypatch):
    # Non-artwork: author placed the image mid-article → show it in place, no top lead.
    monkeypatch.setattr(main.lead_image_service, "get_feed_strategy", lambda u: ("auto", 0.0, False))
    content, lead = _strip(f'<p>intro paragraph</p><p><img src="{LEAD}"></p><p>more</p>')
    assert lead is None
    assert LEAD in content            # left in its natural position


def test_artwork_hoists_image_to_top(monkeypatch):
    # Artwork feeds: strip the in-body image so it shows once at the top.
    monkeypatch.setattr(main.lead_image_service, "get_feed_strategy", lambda u: ("artwork", 0.0, True))
    content, lead = _strip(f'<p>description first</p><figure><img src="{LEAD}"></figure>')
    assert lead == LEAD
    assert LEAD not in (content or "")


def test_tumblr_size_variant_dedup():
    lead = "https://64.media.tumblr.com/abc123/def456/s1280x1920/pic.jpg"
    body_variant = "https://64.media.tumblr.com/abc123/def456/s640x960/pic.jpg"
    # Mid-content (not the opener, so the opener-strip path doesn't fire first).
    content, out_lead = _strip(f'<p>intro text</p><p><img src="{body_variant}"></p>', lead=lead)
    # Same media hash/token prefix → treated as the same photo, drop the lead.
    assert out_lead is None


def test_thumbnail_wrapper_imgs_stripped(monkeypatch):
    # Lead from source scrape (not in body); body is just a thumbnail + tiny text.
    monkeypatch.setattr(main.lead_image_service, "get_feed_strategy", lambda u: ("og_scrape", 0.0, True))
    content, lead = _strip('<p><img src="https://cdn.test/thumb.jpg"> short</p>')
    assert lead == LEAD
    assert "<img" not in (content or "")


# --- block-spacer collapse -------------------------------------------------
def test_spacer_only_blocks_and_boundary_breaks_are_dropped():
    """Blogger emits "</div><br/><div><i><br/></i>" between blocks, which renders
    as a large empty gap. It was always there — a second image above it filled
    the space, so removing that image is what made it visible.
    """
    out = main._collapse_block_spacers(
        '<div><img src="https://x.test/a.png"></div><br/><div><i><br/></i><p>Body text</p></div>'
    )
    assert "<br" not in out
    assert "Body text" in out
    assert 'src="https://x.test/a.png"' in out


def test_a_br_between_words_survives():
    """The narrowness is the whole point: plenty of feeds ship no <p> at all and
    separate lines with <br><br>. Collapsing those would reflow real prose."""
    html = "<p>line one<br/><br/>line two</p>"
    assert main._collapse_block_spacers(html) == html


def test_a_block_holding_an_image_is_not_a_spacer():
    """No text, but not empty — dropping it would delete the picture."""
    html = '<div><br/><img src="https://x.test/b.png"></div>'
    out = main._collapse_block_spacers(html)
    assert 'src="https://x.test/b.png"' in out


def test_content_without_breaks_is_returned_untouched():
    html = "<p>nothing to do</p>"
    assert main._collapse_block_spacers(html) == html


def test_all_spacer_content_collapses_to_none():
    assert main._collapse_block_spacers("<div><br/></div>") is None
