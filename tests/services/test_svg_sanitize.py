"""Tests for inline-SVG sanitization (PR5: inline <svg> thumbnails / lead images)."""
from __future__ import annotations

from services.svg_sanitize import sanitize_svg, svg_to_data_uri

# A real-world inline <svg> shape (analogue.co firmware notes) — currentColor path.
ANALOGUE = (
    '<svg viewBox="0 0 105 85" xmlns="http://www.w3.org/2000/svg">'
    '<path clip-rule="evenodd" d="M38 17H66V65Z" fill="currentColor" fill-rule="evenodd"></path>'
    "</svg>"
)


def test_real_inline_svg_kept_with_color_fallback():
    out = sanitize_svg(ANALOGUE)
    assert out is not None
    assert "<path" in out and 'd="M38 17H66V65Z"' in out
    assert "currentColor" in out
    # currentColor gets a visible fallback color on the root since a standalone
    # <img> has no parent to inherit from.
    assert 'color="#888"' in out


def test_data_uri_round_trip():
    uri = svg_to_data_uri(ANALOGUE)
    assert uri is not None
    assert uri.startswith("data:image/svg+xml,")
    assert "%3Csvg" in uri  # url-encoded


def test_strips_onload_on_root():
    out = sanitize_svg('<svg onload="alert(1)"><path d="M0 0"/></svg>')
    assert out is not None
    assert "onload" not in out.lower()


def test_drops_script_subtree():
    out = sanitize_svg('<svg><script>alert(1)</script><path d="M0 0"/></svg>')
    assert out is not None
    assert "<script" not in out.lower() and "alert" not in out


def test_drops_foreignobject_and_iframe():
    out = sanitize_svg(
        '<svg><foreignObject><iframe src="javascript:alert(1)"></iframe>'
        '</foreignObject><path d="M0 0"/></svg>'
    )
    assert out is not None
    assert "foreignobject" not in out.lower()
    assert "iframe" not in out.lower()
    assert "javascript" not in out.lower()


def test_drops_anchor_and_href():
    out = sanitize_svg('<svg><a href="javascript:alert(1)"><path d="M0 0"/></a></svg>')
    assert out is not None
    assert "javascript" not in out.lower()
    # <a> is dropped but its <path> child geometry is preserved.
    assert "<path" in out


def test_drops_external_image_href():
    out = sanitize_svg('<svg><image href="http://evil/x"/><path d="M0 0"/></svg>')
    assert out is not None
    assert "<image" not in out.lower()
    assert "evil" not in out


def test_rejects_external_url_in_fill():
    out = sanitize_svg('<svg><path d="M0 0" fill="url(http://evil/x)"/></svg>')
    assert out is not None
    assert "evil" not in out
    assert "url(http" not in out.lower()


def test_allows_internal_url_fragment():
    out = sanitize_svg('<svg><path d="M0 0" fill="url(#grad)"/></svg>')
    assert out is not None
    assert "url(#grad)" in out


def test_empty_shell_rejected():
    assert sanitize_svg("<svg></svg>") is None
    assert sanitize_svg("") is None
    assert sanitize_svg("<div>not svg</div>") is None
