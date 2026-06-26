"""_looks_like_bbcode must not misclassify genuine HTML as BBCode.

Sphinx/Pelican math blogs (eli.thegreenplace.net) carry LaTeX alt text like
"[I=\\int ...]" / "[s(x)=...]" that reads as a "[i]"/"[s]" BBCode tag. When such
HTML was treated as BBCode, _bbcode_to_html's newline->`<br>` step ran over the
newline-formatted source and shredded every paragraph into one line per break
("poem" layout). Genuine HTML (block tags) must therefore never be seen as BBCode.
"""
from __future__ import annotations

import main

# Eli's Fourier-series content: real <p> structure + LaTeX-bracket alt text that
# trips the raw BBCode signal regex (>=2 matches) but is unmistakably HTML.
_ELI_HTML = (
    '<p>Let’s assume that <img alt="f(x)" class="valign-m4" '
    'src="https://x/f.png"> is well-behaved.</p>\n'
    '<p>Integrating: <img alt="\\[I=\\int x\\cdot sin(kx) dx\\]" class="align-center">\n'
    'and <img alt="\\[I=\\frac{-x\\cdot cos(kx)}{k}\\]" class="align-center">.</p>\n'
)

_NEXUS_BBCODE_WITH_BR = (
    "Check this [b]mod[/b] out!<br/>It is [i]great[/i] and "
    "[url=https://x]here[/url].<br/>Download now."
)
_PURE_BBCODE = "[b]Title[/b]\n[i]desc[/i] and [url=https://x]link[/url]"


def test_html_with_latex_brackets_is_not_bbcode():
    # The raw signal still fires (>=2), but the block-tag guard wins.
    assert len(main._BBCODE_SIGNAL_RE.findall(_ELI_HTML)) >= 2
    assert main._looks_like_bbcode(_ELI_HTML) is False


def test_real_bbcode_still_detected():
    assert main._looks_like_bbcode(_NEXUS_BBCODE_WITH_BR) is True
    assert main._looks_like_bbcode(_PURE_BBCODE) is True


def test_html_content_keeps_newlines_unconverted():
    # End-to-end guard: HTML content must not gain <br> per source newline.
    assert "<br" not in _ELI_HTML.lower()
    if main._looks_like_bbcode(_ELI_HTML):  # would only run for true BBCode
        converted = main._bbcode_to_html(_ELI_HTML)
        assert "<br" not in converted.lower(), "HTML newlines were turned into <br>"
