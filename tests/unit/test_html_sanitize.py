"""html_sanitize is Lectio's single allowlist sanitizer. It must keep safe
embeds (iframes from trusted hosts, sanitized SVG/MathML, audio/video) while
removing scripts, event handlers, and unsafe URLs — Lectio owns sanitization now
that feeds are parsed with feedparser sanitization disabled."""
from __future__ import annotations

import pytest

from services import html_sanitize as H


@pytest.mark.parametrize("payload", [
    '<script>alert(1)</script>',
    '<img src=x onerror=alert(1)>',
    '<a href="javascript:alert(1)">x</a>',
    '<a href="vbscript:msgbox(1)">x</a>',
    '<iframe src="data:text/html,<script>alert(1)</script>"></iframe>',
    '<form action="https://evil"><input name=x></form>',
    '<object data="https://evil/x.swf"></object>',
    '<svg><script>alert(1)</script></svg>',
])
def test_dangerous_content_removed(payload):
    out = H.sanitize_html(payload).lower()
    assert "alert" not in out
    assert "onerror" not in out
    assert "javascript:" not in out
    assert "vbscript:" not in out


def test_basic_formatting_kept():
    out = H.sanitize_html("<p>Hello <strong>world</strong> <a href='https://x.test'>link</a></p>")
    assert "<strong>world</strong>" in out
    assert 'href="https://x.test"' in out


def test_img_leadimage_attrs_kept():
    # width/height (lead-image scorer) and data-* lazyload sources (extractor) must
    # survive — stripping them broke lead images on inline feeds.
    out = H.sanitize_html(
        '<img src="ph.gif" data-src="https://x.test/real.jpg" width="800" height="600" '
        'srcset="https://x.test/r2.jpg 2x" sizes="100vw" loading="lazy">'
    )
    for frag in ('width="800"', 'height="600"', 'data-src="https://x.test/real.jpg"', 'sizes="100vw"'):
        assert frag in out, frag


def test_img_unsafe_data_src_dropped():
    # data-src is scheme-validated so it can't smuggle javascript: into a later src swap.
    out = H.sanitize_html('<img src="ok.jpg" data-src="javascript:alert(1)">')
    assert "javascript:" not in out
    assert 'src="ok.jpg"' in out


def test_class_kept_id_dropped():
    # class is kept so content-cleanup passes (related-block strip, etc.) work;
    # id is dropped to avoid colliding with the app's own element IDs.
    out = H.sanitize_html('<div class="related alignright" id="entry-body">x</div>')
    assert 'class="related alignright"' in out
    assert "id=" not in out


@pytest.mark.parametrize("host", [
    "https://www.youtube.com/embed/abc",
    "https://www.youtube-nocookie.com/embed/abc",
    "https://player.vimeo.com/video/123",
    "https://w.soundcloud.com/player/?url=x",
    "https://bandcamp.com/EmbeddedPlayer/album=1",
    "https://open.spotify.com/embed/track/1",
    "https://codepen.io/x/embed/y",
])
def test_trusted_embeds_kept_and_sandboxed(host):
    out = H.sanitize_html(f'<iframe src="{host}"></iframe>')
    assert host in out
    assert "sandbox=" in out
    assert "referrerpolicy=" in out


@pytest.mark.parametrize("host", [
    "https://evil.com/x",
    "https://notyoutube.com/embed/x",     # suffix-confusion guard
    "https://youtube.com.evil.com/embed/x",
    "http://www.youtube.com/embed/x",      # must be https
])
def test_untrusted_or_insecure_embeds_dropped(host):
    out = H.sanitize_html(f'<iframe src="{host}"></iframe>')
    assert "<iframe" not in out.lower()


def test_iframe_event_handlers_and_extra_attrs_stripped():
    out = H.sanitize_html(
        '<iframe src="https://www.youtube.com/embed/abc" onload="alert(1)" '
        'srcdoc="<script>x</script>" style="x"></iframe>'
    )
    assert "onload" not in out.lower()
    assert "srcdoc" not in out.lower()
    assert "style=" not in out.lower()


def test_inline_svg_kept_but_cleaned():
    out = H.sanitize_html('<svg viewBox="0 0 10 10"><rect width="10" height="10"/>'
                          '<script>alert(1)</script></svg>')
    assert "<svg" in out.lower()
    assert "alert" not in out.lower()


def test_mathml_kept_attributes_stripped():
    out = H.sanitize_html('<math><mrow><mi onclick="x">a</mi><mo>+</mo><mn>2</mn></mrow></math>')
    assert "<math>" in out.lower().replace(' ', '') or "<math" in out.lower()
    assert "<mi>a</mi>" in out.lower().replace(' ', '') or "<mi" in out.lower()
    assert "onclick" not in out.lower()


def test_math_svg_object_height_promoted():
    # Sphinx/dvisvgm inline math: <object> becomes <img>, the true rendered px height
    # is lifted off the (stripped) inline style onto a real height attr, and the
    # valign baseline class is preserved.
    out = H.sanitize_html(
        '<object type="image/svg+xml" data="https://x.test/a_n.svg" '
        'class="valign-m3" style="height: 11px;">a_n</object>'
    ).lower()
    assert "<img" in out and "<object" not in out
    assert 'height="11"' in out
    assert "valign-m3" in out and "lectio-math-svg" in out
    assert 'alt="a_n"' in out
    assert "style=" not in out


def test_math_png_img_height_promoted():
    # Pre-existing PNG inline math <img> keeps its valign class and gains a height
    # attr from the inline style (which is then stripped).
    out = H.sanitize_html(
        '<img class="valign-m4" src="https://x.test/f.png" style="height: 18px;">'
    ).lower()
    assert 'height="18"' in out
    assert "valign-m4" in out
    assert "style=" not in out


def test_math_block_equation_height_promoted():
    out = H.sanitize_html(
        '<object type="image/svg+xml" data="https://x.test/eq.svg" '
        'class="align-center" style="height: 49px;">\\[f(x)\\]</object>'
    ).lower()
    assert "<img" in out
    assert 'height="49"' in out
    assert "align-center" in out


def test_pseudo_html_in_text_reparsed():
    # Feeds sometimes store entity-escaped tags as literal text; they should
    # become real elements so they render correctly.
    out = H.sanitize_html("<p>&lt;em&gt;Title&lt;/em&gt; and &lt;strong&gt;body&lt;/strong&gt;</p>")
    assert "<em>title</em>" in out.lower()
    assert "<strong>body</strong>" in out.lower()


def test_pseudo_html_in_code_kept_literal():
    # Inside <code>/<pre>, escaped tags must stay as literal text.
    out = H.sanitize_html("<pre><code>&lt;em&gt;not-a-tag&lt;/em&gt;</code></pre>")
    assert "<em>" not in out.lower()
    assert "&lt;em&gt;" in out or "not-a-tag" in out


def test_pseudo_html_disallowed_attrs_stripped_after_reparse():
    # Event handlers must be stripped even when the tag arrives entity-escaped.
    out = H.sanitize_html('<p>&lt;em onclick="evil()"&gt;text&lt;/em&gt;</p>')
    assert "onclick" not in out.lower()
    assert "<em>" in out.lower() or "text" in out.lower()


def test_pseudo_html_no_document_wrapper():
    # The reparsing must not introduce <html> or <body> wrapper elements.
    out = H.sanitize_html("<p>&lt;em&gt;hi&lt;/em&gt;</p>")
    assert "<html" not in out.lower()
    assert "<body" not in out.lower()


def test_audio_video_kept():
    out = H.sanitize_html('<video src="https://x.test/v.mp4" controls></video>'
                          '<audio src="https://x.test/a.mp3" controls></audio>')
    assert "<video" in out and "<audio" in out


def test_empty_and_none_safe():
    assert H.sanitize_html("") == ""
    assert H.sanitize_html(None) is None  # ty: ignore[invalid-argument-type]


# --- inline style: enumerated allowlist ------------------------------------

def test_presentational_styles_survive():
    """Author formatting the app can't otherwise recover: feed CSS is never
    loaded, so a stripped text-align is a stripped intent."""
    out = H.sanitize_html('<p style="text-align:center">c</p>')
    assert 'style="text-align: center"' in out
    out = H.sanitize_html('<span style="font-style:italic;font-weight:bold">x</span>')
    assert "font-style: italic" in out and "font-weight: bold" in out


def test_style_values_are_normalized_for_the_stylesheet():
    """The emitted form is ours, not the feed's — style.css keys its
    centering rules off the exact string `text-align: center`."""
    for raw in ("TEXT-ALIGN:CENTER", "text-align:   center", "text-align: center !important"):
        assert 'style="text-align: center"' in H.sanitize_html(f'<p style="{raw}">c</p>')


def test_unlisted_style_properties_are_dropped():
    """Layout/positioning would let feed content escape the pane or overlay the
    app's own UI, which matters even with no scripting involved."""
    for css in ("position:fixed;top:0;left:0", "z-index:99999", "width:5000px",
                "display:none", "opacity:0", "content:attr(data-x)"):
        assert "style=" not in H.sanitize_html(f'<p style="{css}">x</p>')


def test_style_payloads_cannot_survive():
    """Values are matched against a literal allowlist, so there is nowhere for a
    payload to hide — nothing free-form is ever kept."""
    for css in (
        "background:url(javascript:alert(1))",
        "width:expression(alert(1))",
        "behavior:url(x.htc)",
        "-moz-binding:url(x.xml)",
        "text-align:url(javascript:alert(1))",
        "text-align:center\\3b background:red",
    ):
        out = H.sanitize_html(f'<p style="{css}">x</p>')
        assert "javascript" not in out.lower()
        assert "expression" not in out.lower()
        assert "binding" not in out.lower()
        assert "url(" not in out.lower()


def test_valid_declaration_survives_alongside_a_payload():
    """A mixed declaration keeps only the listed part — the rest is dropped,
    not escaped-and-kept."""
    out = H.sanitize_html('<p style="text-align:center;background:url(javascript:alert(1))">x</p>')
    assert 'style="text-align: center"' in out
    assert "javascript" not in out.lower() and "background" not in out.lower()


def test_center_tag_survives():
    """<center> is deprecated but purely presentational; unwrapping it silently
    lost the author's centering."""
    out = H.sanitize_html("<center>c</center>")
    assert "<center>" in out and "c" in out


# ── Off-site links leave the tab ──────────────────────────────────────────────
# Following an article link in place replaces Lectio and loses your position in
# the list. Enforced here rather than in the browser because this is the one
# choke point every rendered body passes through — Read Mode loads none of
# app.js, and offline captures are rendered from stored HTML.

def test_external_link_opens_in_a_new_tab_with_noopener():
    out = H.sanitize_html('<a href="https://example.com/post">x</a>')
    assert 'target="_blank"' in out
    # Without noopener the opened page can rewrite this tab out from under the
    # reader (reverse tabnabbing).
    assert 'rel="noopener noreferrer"' in out


def test_in_page_fragment_link_stays_in_the_tab():
    """Footnotes and tables of contents are same-page anchors — sending them to a
    new tab would break them outright."""
    out = H.sanitize_html('<a href="#footnote-3">3</a>')
    assert "target=" not in out


@pytest.mark.parametrize("href", ["mailto:someone@example.com", "tel:+15551234"])
def test_handler_schemes_stay_in_the_tab(href):
    """A mail or dialer handler would otherwise be handed a blank tab it has no
    way to close."""
    out = H.sanitize_html(f'<a href="{href}">x</a>')
    assert "target=" not in out


def test_a_feed_cannot_choose_its_own_target_or_drop_noopener():
    """target/rel are in the allowlist only so the second sanitizing pass keeps
    what the first pass set — the values are always ours."""
    out = H.sanitize_html(
        '<a href="https://example.com/x" target="_self" rel="opener">x</a>'
    )
    assert 'target="_blank"' in out
    assert 'rel="noopener noreferrer"' in out
    assert 'target="_self"' not in out


def test_a_feed_cannot_smuggle_a_target_onto_a_fragment_link():
    out = H.sanitize_html('<a href="#x" target="_blank" rel="opener">x</a>')
    assert "target=" not in out
    assert "rel=" not in out
