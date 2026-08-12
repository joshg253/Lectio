"""One illegal byte must not cost a whole feed.

reader parses with a strict SAX parser, so a single character XML 1.0 forbids
makes the entire document not well-formed. inventwithpython.com shipped a raw
0x0B mid-sentence ("…interesting talk.\\x0bHi. I'm Al Sweigart…") and its 2.7MB,
100-entry feed failed outright with "not well-formed (invalid token)" at line
19918 — while feedparser read the same bytes happily, which is why the feed
looked healthy right up until reader refused it.

Only the characters XML actually forbids are removed. Tab, newline and carriage
return are legal and must survive untouched, or the scrub would corrupt every
feed it touched to fix the rare broken one.
"""
from __future__ import annotations

import pytest

from services.reader_api import _XML_ILLEGAL_BYTES_RE as SCRUB


def _clean(b: bytes) -> bytes:
    return SCRUB.sub(b"", b)


def test_the_vertical_tab_that_broke_the_feed_is_removed():
    assert _clean(b"talk.\x0bHi. I'm Al") == b"talk.Hi. I'm Al"


@pytest.mark.parametrize("byte", [b"\x00", b"\x01", b"\x08", b"\x0b", b"\x0c", b"\x0e", b"\x1f"])
def test_every_forbidden_control_byte_is_removed(byte):
    assert _clean(b"<title>a" + byte + b"b</title>") == b"<title>ab</title>"


@pytest.mark.parametrize("byte", [b"\x09", b"\x0a", b"\x0d"])
def test_the_three_legal_whitespace_controls_survive(byte):
    """Tab, LF and CR are valid XML — stripping them would mangle every feed."""
    payload = b"<title>a" + byte + b"b</title>"
    assert _clean(payload) == payload


def test_an_already_valid_feed_is_byte_identical():
    payload = b'<?xml version="1.0"?>\n<rss><channel>\n\t<title>Fine</title>\r\n</channel></rss>'
    assert _clean(payload) == payload


def test_high_bytes_are_untouched():
    """UTF-8 multibyte sequences live above 0x7f and must not be caught."""
    payload = "<title>Café — naïve</title>".encode()
    assert _clean(payload) == payload
