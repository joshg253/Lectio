"""_clean_qwantz_content strips Dinosaur Comics nav chrome, keeping the comic
image and the dated author commentary."""
from __future__ import annotations

import main


# Shape mirrors the real qwantz.com/rssfeed.php description (unescaped).
QWANTZ = (
    '<center><table width=740 border=0 cellspacing=5 cellpadding=5>'
    '<tr><td colspan=4 align="center"></td></tr>'
    '<tr><td colspan=4 align="center">'
    '<a href="http://www.qwantz.com/archive.php">archive</a> - '
    '<a href="mailto:ryan@qwantz.com">contact</a> - '
    '<a href="http://www.topatoco.com/qwantz">sexy exciting merchandise</a> - '
    '<a href="http://www.ohnorobot.com/index.php?comic=23">search</a> - '
    '<a href="http://www.qwantz.com/about.php">about</a>'
    '</td></tr></table>'
    '<img src="http://www.qwantz.com/comics/comic2-5184.png" class="comic" '
    'title="secret hover text here">'
    '<table width=740 border=0 cellspacing=5 cellpadding=5>'
    '<tr><td width=100 align="left"><a rel="prev" href="x">&larr; previous</a></td>'
    '<td align="center">June 19th, 2026</td>'
    '<td width=100 align="right">next</td></tr>'
    '<tr><td colspan=3 align="left"><P><b>June 19th, 2026: </b>'
    'I guess this comic was inspired by horses too.</p></td></tr>'
    '</table></center>'
)


def test_keeps_comic_and_commentary():
    out = main._clean_qwantz_content(QWANTZ)
    assert "comic2-5184.png" in out          # comic image kept
    assert "secret hover text here" in out   # title (secret text) preserved
    assert "inspired by horses too" in out   # commentary kept


def test_strips_nav_chrome():
    out = main._clean_qwantz_content(QWANTZ)
    assert "archive.php" not in out          # top nav gone
    assert "topatoco" not in out             # merch nav gone
    assert "ohnorobot" not in out            # search nav gone
    assert 'rel="prev"' not in out           # prev/next nav gone
    assert "&larr; previous" not in out
    assert "<table" not in out               # no nav tables remain


def test_non_qwantz_is_noop():
    html = "<p>A normal article with no comic.</p>"
    assert main._clean_qwantz_content(html) == html
