"""The whole process must parse feeds with one feedparser: the installed one.

`reader` defaults to its vendored copy (reader._vendor.feedparser), which is
pinned older than the installed feedparser and — since feedparser 6.0.13 dropped
the sgmllib3k dependency the vendored copy's bare `import sgmllib` relied on —
no longer imports at all. services/__init__.py opts out via
READER_NO_VENDORED_FEEDPARSER; these tests fail loudly if that stops taking
effect, because the symptom otherwise is feeds quietly parsing differently.
"""
from __future__ import annotations

import feedparser
from reader._parser.feedparser import feedparser as reader_feedparser

import main


def test_reader_uses_the_installed_feedparser():
    assert reader_feedparser is feedparser


def test_reader_is_not_using_the_vendored_copy():
    assert "_vendor" not in reader_feedparser.__file__


def test_month_first_date_handler_applies_to_reader_ingest():
    # main.py registers _parse_month_first_pubdate for dates like
    # "May 11, 2026 19:15:50 +0000" (RFC 2822 without the day-of-week). It is
    # registered on the installed feedparser, so it only reaches ingest while
    # reader is using that same module.
    assert main._parse_month_first_pubdate in reader_feedparser.datetimes._date_handlers
    assert reader_feedparser.datetimes._parse_date("May 11, 2026 19:15:50 +0000") is not None
