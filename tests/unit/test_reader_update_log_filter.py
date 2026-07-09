"""reader logs every failed update_feed at ERROR with a full traceback, but
Lectio already catches each one (backoff + problematic-feeds panel). The
_ReaderUpdateFetchErrorFilter demotes those expected fetch/parse failures to a
one-line WARNING while leaving genuinely unexpected errors loud."""
from __future__ import annotations

import logging
import sys

import main


def _record(msg: str, level: int = logging.ERROR, with_exc: bool = True) -> logging.LogRecord:
    exc_info = None
    if with_exc:
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            exc_info = sys.exc_info()
    return logging.LogRecord("reader.update", level, "x.py", 1, msg, None, exc_info)


def _apply(msg: str, **kw) -> logging.LogRecord:
    r = _record(msg, **kw)
    main._ReaderUpdateFetchErrorFilter().filter(r)
    return r


def test_429_demoted_to_warning_without_traceback():
    r = _apply('updated  status=error feed=https://old.reddit.com/r/x/.rss '
               'exception="ParseError: bad HTTP status code: 429 Client Error: Too Many Requests"')
    assert r.levelname == "WARNING"
    assert r.exc_info is None


def test_timeout_demoted():
    r = _apply('updated  status=error feed=https://x exception="ConnectTimeout: timed out"')
    assert r.levelname == "WARNING"


def test_dead_feed_404_demoted():
    r = _apply('updated  status=error feed=https://x exception="ParseError: bad HTTP status code: 404 Not Found"')
    assert r.levelname == "WARNING"


def test_unexpected_error_stays_error_with_traceback():
    r = _apply('updated  status=error feed=https://x exception="StorageError: disk I/O error"')
    assert r.levelname == "ERROR"
    assert r.exc_info is not None


def test_non_update_error_untouched():
    r = _apply("some other reader error entirely")
    assert r.levelname == "ERROR"


def test_info_line_untouched():
    r = _apply("updated  status=ok feed=https://x", level=logging.INFO, with_exc=False)
    assert r.levelname == "INFO"
