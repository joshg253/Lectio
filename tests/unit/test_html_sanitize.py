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


def test_audio_video_kept():
    out = H.sanitize_html('<video src="https://x.test/v.mp4" controls></video>'
                          '<audio src="https://x.test/a.mp3" controls></audio>')
    assert "<video" in out and "<audio" in out


def test_empty_and_none_safe():
    assert H.sanitize_html("") == ""
    assert H.sanitize_html(None) is None  # ty: ignore[invalid-argument-type]
